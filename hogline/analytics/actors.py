"""Bounded perspective person observations and immutable anonymous fragments."""

import hashlib
import json
import math
from uuid import NAMESPACE_URL, uuid5

import pyarrow as pa
import pyarrow.parquet as pq
from sqlalchemy import select

from hogline.adapters.sources import digest
from hogline.analytics.dense import save_dense
from hogline.db import AnalysisWindow, WindowActors, now
from hogline.perception.actors import VERSION, actor_fragments
from hogline.perception.people import MAX_PEOPLE, MODEL_SHA256
from hogline.perception.people import VERSION as DETECTOR_VERSION
from hogline.perception.perspective_stones import VERSION as PERSPECTIVE_HANDLES_VERSION
from hogline.perception.pose import MODEL_SHA256 as POSE_SHA256
from hogline.perception.pose import VERSION as POSE_VERSION

LEGACY_SCHEMA_VERSION = "hogline-perspective-people-v1"
SCHEMA_VERSION = "hogline-perspective-people-v2"
SCHEMA = pa.schema(
    [
        ("sample_index", pa.int32()),
        ("source_id", pa.string()),
        ("source_metadata_sha256", pa.string()),
        ("profile_sha256", pa.string()),
        ("pts", pa.int64()),
        ("time_base_num", pa.int32()),
        ("time_base_den", pa.int32()),
        ("source_seconds", pa.float64()),
        ("requested_interval_seconds", pa.float64()),
        ("decoded_rgb_sha256", pa.string()),
        ("youtube_url", pa.string()),
        ("view", pa.string()),
        ("crop_width", pa.int32()),
        ("crop_height", pa.int32()),
        (
            "people",
            pa.list_(
                pa.struct(
                    [
                        ("bbox_px", pa.list_(pa.float64(), 4)),
                        ("raw_model_score", pa.float64()),
                        ("score_kind", pa.string()),
                        ("boundary_clipped", pa.bool_()),
                        ("torso_hsv_histogram", pa.list_(pa.float64())),
                    ]
                )
            ),
        ),
    ]
)
MAX_RESULT_BYTES = 2 * 1024**2
LEGACY_SCHEMA = SCHEMA
POSE = pa.struct(
    [
        ("status", pa.string()),
        ("missing_reason", pa.string()),
        ("raw_model_score", pa.float64()),
        (
            "landmarks",
            pa.list_(
                pa.struct(
                    [
                        ("index", pa.int32()),
                        ("center_px", pa.list_(pa.float64(), 2)),
                        ("raw_visibility", pa.float64()),
                        ("raw_presence", pa.float64()),
                        ("inside_crop", pa.bool_()),
                    ]
                )
            ),
        ),
    ]
)
SCHEMA = pa.schema(
    [
        *[field for field in LEGACY_SCHEMA if field.name != "people"],
        (
            "people",
            pa.list_(
                pa.struct(
                    [
                        *LEGACY_SCHEMA.field("people").type.value_type,
                        ("body_pose", POSE),
                    ]
                )
            ),
        ),
        (
            "perspective_handles",
            pa.struct(
                [
                    ("status", pa.string()),
                    ("missing_reason", pa.string()),
                    (
                        "detections",
                        pa.list_(
                            pa.struct(
                                [
                                    ("color", pa.string()),
                                    ("center_px", pa.list_(pa.float64(), 2)),
                                    ("bbox_px", pa.list_(pa.int32(), 4)),
                                    ("area_px2", pa.float64()),
                                    ("shape_score", pa.float64()),
                                    ("granite_context_fraction", pa.float64()),
                                    ("person_overlap", pa.bool_()),
                                    ("near_wrist", pa.bool_()),
                                    ("boundary_clipped", pa.bool_()),
                                ]
                            )
                        ),
                    ),
                ]
            ),
        ),
    ]
)


def validate_pose_and_handles(row):
    for person in row["people"]:
        pose = person.get("body_pose")
        if pose is None:
            continue  # Older bounded artifacts predate pose inference.
        points = pose["landmarks"]
        if (
            pose["status"] not in ("observed", "unavailable")
            or (pose["status"] == "observed" and {p["index"] for p in points} != set(range(11, 33)))
            or (pose["status"] != "observed" and points)
            or len(points) > 22
            or (pose["raw_model_score"] is not None and not 0 <= pose["raw_model_score"] <= 1)
            or any(
                len(p["center_px"]) != 2
                or not all(math.isfinite(v) for v in p["center_px"])
                or not 0 <= p["raw_visibility"] <= 1
                or not 0 <= p["raw_presence"] <= 1
                for p in points
            )
        ):
            raise ValueError("Actor input contains invalid body-pose observations")
    observation = row.get("perspective_handles")
    if observation is None:
        return
    if observation["status"] not in ("observed", "unavailable") or len(observation["detections"]) > 128:
        raise ValueError("Perspective handle observations exceed their bounded contract")
    for d in observation["detections"]:
        if (
            d["color"] not in ("red", "yellow")
            or len(d["center_px"]) != 2
            or not all(
                math.isfinite(v) and 0 <= v < bound
                for v, bound in zip(d["center_px"], (row["crop_width"], row["crop_height"]), strict=True)
            )
            or not 0 <= d["granite_context_fraction"] <= 1
            or not 0 <= d["shape_score"] <= 1
        ):
            raise ValueError("Actor input contains invalid perspective-handle observations")


def validate_actor_rows(window, rows):
    if not 1 <= window["sample_count"] <= 240 or len(rows) != 2 * window["sample_count"]:
        raise ValueError("Actor input must contain both perspective views of a bounded window")
    seen, times = set(), {}
    for row in rows:
        key = (row["sample_index"], row["view"])
        if (
            key in seen
            or row["view"] not in ("perspective_left", "perspective_right")
            or not (0 <= row["sample_index"] < window["sample_count"])
        ):
            raise ValueError("Actor input has duplicate or missing sample/view rows")
        seen.add(key)
        for field in ("source_id", "source_metadata_sha256", "profile_sha256"):
            if row[field] != window[field]:
                raise ValueError("Actor input source/crop provenance disagrees")
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
            raise ValueError("Actor input has invalid source timestamps or crop geometry")
        timing = (seconds, row["pts"], row["time_base_num"], row["time_base_den"], row["decoded_rgb_sha256"])
        if times.setdefault(row["sample_index"], timing) != timing:
            raise ValueError("Actor views disagree about the original frame")
        if len(row["people"]) > MAX_PEOPLE:
            raise ValueError("Actor candidate count exceeds the bounded model contract")
        for person in row["people"]:
            box, histogram = person["bbox_px"], person["torso_hsv_histogram"]
            if (
                len(box) != 4
                or not all(math.isfinite(x) for x in box)
                or box[0] < 0
                or box[1] < 0
                or min(box[2:]) <= 0
                or box[0] + box[2] > row["crop_width"] + 0.001
                or box[1] + box[3] > row["crop_height"] + 0.001
                or not math.isfinite(person["raw_model_score"])
                or not 0 <= person["raw_model_score"] <= 1
                or (
                    histogram is not None
                    and (
                        len(histogram) != 32
                        or not all(math.isfinite(x) and 0 <= x <= 1 for x in histogram)
                        or abs(sum(histogram) - 1) > 0.001
                    )
                )
            ):
                raise ValueError("Actor input contains invalid body geometry or torso descriptor")
        validate_pose_and_handles(row)
    ordered = [times[i] for i in range(window["sample_count"])]
    if any(
        b[0] <= a[0] or abs((b[1] * b[2] / b[3] - a[1] * a[2] / a[3]) - (b[0] - a[0])) > 1e-4
        for a, b in zip(ordered, ordered[1:], strict=False)
    ):
        raise ValueError("Actor input timestamps are inconsistent with original PTS")


def save_actor_observations(rows, manifest, root, max_bytes, budget):
    return save_dense(rows, manifest, root, max_bytes, budget, schema=SCHEMA, schema_version=SCHEMA_VERSION)


def load_actor_input(root, window):
    info = window.get("actor_input", {})
    if info.get("status") != "available":
        raise ValueError("Perspective person observations are unavailable; reanalyze with the pinned model")
    artifact = info["parquet"]
    path = (root / artifact["path"]).resolve()
    if (
        artifact["schema"] not in (SCHEMA_VERSION, LEGACY_SCHEMA_VERSION)
        or not path.is_relative_to((root / "dense").resolve())
        or not path.is_file()
        or path.stat().st_size > 16 * 1024**2
    ):
        raise ValueError("Perspective observations are missing or outside their bounded data directory")
    content = path.read_bytes()
    if len(content) != artifact["bytes"] or hashlib.sha256(content).hexdigest() != artifact["sha256"]:
        raise ValueError("Perspective observation checksum mismatch")
    parquet = pq.ParquetFile(pa.BufferReader(content))
    if parquet.metadata.num_rows != artifact["rows"] or parquet.metadata.num_rows > 480:
        raise ValueError("Perspective row count exceeds the bounded window contract")
    metadata = parquet.schema_arrow.metadata or {}
    if metadata.get(b"hogline.schema_version", b"").decode() != artifact["schema"]:
        raise ValueError("Perspective schema metadata disagrees with its manifest")
    manifest = json.loads(metadata[b"hogline.input_manifest"])
    if artifact["schema"] == SCHEMA_VERSION and any(
        manifest.get(key) != value
        for key, value in (
            ("pose_version", POSE_VERSION),
            ("pose_model_sha256", POSE_SHA256),
            ("perspective_handles_version", PERSPECTIVE_HANDLES_VERSION),
        )
    ):
        raise ValueError("Perspective feature versions disagree with their pinned manifest")
    if any(
        manifest.get(key) != value
        for key, value in (
            ("source_hash", window["source_metadata_sha256"]),
            ("profile_sha256", window["profile_sha256"]),
            ("request", window["request"]),
            ("detector_version", DETECTOR_VERSION),
            ("model_sha256", MODEL_SHA256),
        )
    ):
        raise ValueError("Perspective manifest disagrees with its retained window")
    rows = parquet.read().to_pylist()
    validate_actor_rows(window, rows)
    return rows


def checked_actor_result(row):
    if digest(row.document) != row.sha256:
        raise ValueError("Stored actor-fragment checksum mismatch")
    return {"id": row.id, "sha256": row.sha256, **row.document}


def save_actors_in_session(session, window, rows):
    identifier = str(uuid5(NAMESPACE_URL, f"window-actors:{window['id']}:{VERSION}"))
    existing = session.get(WindowActors, identifier)
    if existing:
        result = checked_actor_result(existing)
        if result["input"] != window.get("actor_input") or any(
            result[key] != window[key]
            for key in (
                "source_id",
                "source_metadata_sha256",
                "profile_sha256",
            )
        ):
            raise ValueError("Stored actor fragments disagree with their immutable input")
        return result
    info = window["actor_input"]
    result = {"version": VERSION, "tracks": [], "diagnostics": {}, "notice": "Person analysis unavailable."}
    if info["status"] == "available":
        validate_actor_rows(window, rows)
        result = actor_fragments(rows, info["parquet"]["sha256"])
    document = {
        "schema_version": "hogline-actor-fragments-v1",
        "created_at": now(),
        "window_id": window["id"],
        "source_id": window["source_id"],
        "source_metadata_sha256": window["source_metadata_sha256"],
        "profile_sha256": window["profile_sha256"],
        "detector_version": DETECTOR_VERSION,
        "input": info,
        "context_anchor_evidence_ids": window["anchor_evidence_ids"],
        "status": "derived" if info["status"] == "available" else "unavailable",
        **result,
    }
    if len(json.dumps(document).encode()) > MAX_RESULT_BYTES:
        document["status"] = "result_limit_exceeded"
        document["diagnostics"]["fragments_excluded_by_result_limit"] = len(document["tracks"])
        document["tracks"] = []
        document["notice"] = "Actor result exceeded two MiB; perspective observations remain available."
    row = WindowActors(
        id=identifier, window_id=window["id"], version=VERSION, sha256=digest(document), document=document
    )
    session.add(row)
    session.flush()
    return checked_actor_result(row)


def derive_window_actors(db, window_id):
    with db.snapshot_session(write=True) as session:
        row = session.get(AnalysisWindow, window_id)
        if row is None:
            raise ValueError("Detailed window not found")
        window = {**row.document, "id": row.id, "source_id": row.source_id}
        return save_actors_in_session(session, window, load_actor_input(db.settings.data_dir, window))


def actors_in_session(session, *, full=False):
    output = []
    for row in session.scalars(select(WindowActors).order_by(WindowActors.id)):
        result = checked_actor_result(row)
        if not full:
            result["track_count"] = len(result.pop("tracks"))
        output.append(result)
    return output
