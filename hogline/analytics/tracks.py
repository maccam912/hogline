"""Derive immutable handle fragments from bounded, checksum-verified Parquet."""

import hashlib
import json
import math
from uuid import NAMESPACE_URL, uuid5

import pyarrow as pa
import pyarrow.parquet as pq
from sqlalchemy import select

from hogline.adapters.sources import digest
from hogline.analytics.dense import SCHEMA_VERSION
from hogline.db import AnalysisWindow, WindowTracks, now
from hogline.perception.stones import VERSION as DETECTOR_VERSION
from hogline.perception.tracking import VERSION, track_handles
from hogline.perception.visibility import VERSION as VISIBILITY_VERSION

MAX_RESULT_BYTES = 2 * 1024**2


def validate_rows(window, rows):
    if not 1 <= window["sample_count"] <= 240 or len(rows) != 2 * window["sample_count"]:
        raise ValueError("Track input must contain both views of a bounded detailed window")
    seen, times = set(), {}
    for row in rows:
        key = (row["sample_index"], row["view"])
        if (
            key in seen
            or row["view"] not in ("house_top", "house_bottom")
            or not 0 <= row["sample_index"] < window["sample_count"]
        ):
            raise ValueError("Track input has duplicate or missing sample/view rows")
        seen.add(key)
        for field, expected in (
            ("source_id", window["source_id"]),
            ("source_metadata_sha256", window["source_metadata_sha256"]),
            ("profile_sha256", window["profile_sha256"]),
            ("model_version", DETECTOR_VERSION),
            ("visibility_version", VISIBILITY_VERSION),
        ):
            if row.get(field) != expected:
                raise ValueError("Track input source, crop, detector or visibility provenance disagrees")
        seconds = row["source_seconds"]
        if (
            not math.isfinite(seconds)
            or not window["request"]["start_seconds"] - 0.001
            <= seconds
            < window["request"]["end_seconds"] + 0.001
            or row["time_base_num"] <= 0
            or row["time_base_den"] <= 0
            or row["crop_width"] < 2
            or row["crop_height"] < 2
            or row["requested_interval_seconds"] != window["request"]["interval_seconds"]
        ):
            raise ValueError("Track input has invalid geometry or original timestamps")
        timing = (seconds, row["pts"], row["time_base_num"], row["time_base_den"], row["decoded_rgb_sha256"])
        if times.setdefault(row["sample_index"], timing) != timing:
            raise ValueError("Track input views disagree about their original source frame")
        for detection in row["detections"]:
            if (
                detection["color"] not in ("red", "yellow")
                or len(detection["center_px"]) != 2
                or not all(
                    math.isfinite(v) and 0 <= v < bound
                    for v, bound in zip(
                        detection["center_px"], (row["crop_width"], row["crop_height"]), strict=True
                    )
                )
            ):
                raise ValueError("Track input contains invalid handle coordinates")
    ordered = [times[i] for i in range(window["sample_count"])]
    for before, after in zip(ordered, ordered[1:], strict=False):
        if (
            after[0] <= before[0]
            or abs(
                (after[1] * after[2] / after[3] - before[1] * before[2] / before[3]) - (after[0] - before[0])
            )
            > 1e-4
        ):
            raise ValueError("Track input timestamps are nonmonotonic or inconsistent with PTS")


def load_track_input(root, window):
    artifact = window["parquet"]
    path = (root / artifact["path"]).resolve()
    if (
        artifact["schema"] != SCHEMA_VERSION
        or not path.is_relative_to((root / "dense").resolve())
        or not path.is_file()
        or path.stat().st_size > 16 * 1024**2
    ):
        raise ValueError("Current dense observations are unavailable; reanalyze or restore this window")
    content = path.read_bytes()
    if len(content) != artifact["bytes"] or hashlib.sha256(content).hexdigest() != artifact["sha256"]:
        raise ValueError("Dense observation checksum mismatch")
    parquet = pq.ParquetFile(pa.BufferReader(content))
    if parquet.metadata.num_rows != artifact["rows"] or parquet.metadata.num_rows > 480:
        raise ValueError("Dense row count exceeds the bounded window contract")
    metadata = parquet.schema_arrow.metadata or {}
    if metadata.get(b"hogline.schema_version", b"").decode() != SCHEMA_VERSION:
        raise ValueError("Dense schema metadata disagrees with its manifest")
    manifest = json.loads(metadata[b"hogline.input_manifest"])
    for key, expected in (
        ("source_hash", window["source_metadata_sha256"]),
        ("profile_sha256", window["profile_sha256"]),
        ("perception_version", window["model_version"]),
        ("visibility_version", window["visibility_version"]),
        ("request", window["request"]),
    ):
        if manifest.get(key) != expected:
            raise ValueError("Dense input manifest disagrees with the retained window")
    rows = parquet.read().to_pylist()
    validate_rows(window, rows)
    return rows


def save_tracks_in_session(session, window, rows):
    validate_rows(window, rows)
    identifier = str(uuid5(NAMESPACE_URL, f"window-tracks:{window['id']}:{VERSION}"))
    existing = session.get(WindowTracks, identifier)
    if existing:
        if (
            digest(existing.document) != existing.sha256
            or existing.document["input_parquet"]["sha256"] != window["parquet"]["sha256"]
        ):
            raise ValueError("Stored track result disagrees with its immutable input")
        return {"id": existing.id, "sha256": existing.sha256, **existing.document}
    result = track_handles(rows, window["parquet"]["sha256"])
    document = {
        "schema_version": "hogline-handle-tracklets-v1",
        "window_id": window["id"],
        "created_at": now(),
        "source_id": window["source_id"],
        "source_metadata_sha256": window["source_metadata_sha256"],
        "profile_sha256": window["profile_sha256"],
        "detector_version": window["model_version"],
        "visibility_version": window["visibility_version"],
        "input_parquet": window["parquet"],
        "context_anchor_evidence_ids": window["anchor_evidence_ids"],
        **result,
    }
    document["status"] = "derived"
    if len(json.dumps(document).encode()) > MAX_RESULT_BYTES:
        # The optional derivation must not discard a successfully acquired window.
        document["status"] = "result_limit_exceeded"
        document["diagnostics"]["fragments_excluded_by_result_limit"] = len(document["tracks"])
        document["tracks"] = []
        document["notice"] = (
            "Handle fragments exceeded the two-MiB result limit; no partial set is presented. "
            "The original dense observations and retained frames remain available."
        )
    row = WindowTracks(
        id=identifier, window_id=window["id"], version=VERSION, sha256=digest(document), document=document
    )
    session.add(row)
    session.flush()
    return {"id": row.id, "sha256": row.sha256, **document}


def derive_window_tracks(db, window_id):
    with db.snapshot_session(write=True) as session:
        row = session.get(AnalysisWindow, window_id)
        if row is None:
            raise ValueError("Detailed window not found")
        window = {**row.document, "id": row.id, "source_id": row.source_id}
        rows = load_track_input(db.settings.data_dir, window)
        return save_tracks_in_session(session, window, rows)


def tracks_in_session(session, full=False):
    output = []
    for row in session.scalars(select(WindowTracks)):
        if digest(row.document) != row.sha256:
            raise ValueError("Stored handle tracklet checksum mismatch")
        document = row.document
        if not full:
            document = {k: v for k, v in document.items() if k != "tracks"}
            document["track_count"] = len(row.document["tracks"])
            document["moving_fragments"] = sum(
                t["motion_class"] == "position_change_observed" for t in row.document["tracks"]
            )
        output.append({"id": row.id, "sha256": row.sha256, **document})
    return output
