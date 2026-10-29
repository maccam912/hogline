"""Checked scoring samples, empty-house runs and pre-clearing inspection brackets."""

import hashlib
import json
import math

import pyarrow.parquet as pq
from sqlalchemy import select

from hogline.adapters.sources import digest, video_link
from hogline.db import Evidence, ScoringSegment
from hogline.domain.catalog import source_or_error
from hogline.orchestration.ice import SCHEMA, SCHEMA_VERSION


def checked_samples(root, segment):
    if digest({k: v for k, v in segment.items() if k != "sha256"}) != segment.get("sha256"):
        raise ValueError("Scoring segment checksum mismatch")
    artifact = segment["parquet"]
    path = (root / artifact["path"]).resolve()
    if (
        not path.is_relative_to((root / "dense").resolve())
        or not path.is_file()
        or path.stat().st_size != artifact["bytes"]
        or path.stat().st_size > 16 * 1024**2
        or hashlib.sha256(path.read_bytes()).hexdigest() != artifact["sha256"]
    ):
        raise ValueError("Scoring sample artifact missing or corrupt")
    table = pq.read_table(path, page_checksum_verification=True)
    manifest = {
        k: segment[k]
        for k in (
            "source_hash",
            "profile",
            "profile_sha256",
            "stage_version",
            "decoder_version",
            "model_version",
            "references",
            "request",
        )
    }
    metadata = table.schema.metadata or {}
    if (
        artifact["schema"] != SCHEMA_VERSION
        or metadata.get(b"hogline.schema_version") != SCHEMA_VERSION.encode()
        or table.schema.remove_metadata() != SCHEMA
        or json.loads(metadata.get(b"hogline.input_manifest", b"{}")) != manifest
        or len(table) != segment["sample_count"] * 2
        or len(table) != artifact["rows"]
    ):
        raise ValueError("Scoring sample schema or input manifest mismatch")
    rows = table.to_pylist()
    samples = {}
    previous = {}
    for row in rows:
        index, view = row["sample_index"], row["view"]
        if (
            view not in ("house_top", "house_bottom")
            or (index, view) in previous
            or row["time_base_num"] <= 0
            or row["time_base_den"] <= 0
            or not math.isfinite(row["source_seconds"])
            or not segment["request"]["start_seconds"] - 0.001
            <= row["source_seconds"]
            < segment["request"]["end_seconds"] + 0.001
        ):
            raise ValueError("Scoring sample timestamp, view or identity mismatch")
        previous[(index, view)] = True
        timing = {
            k: row[k]
            for k in ("pts", "time_base_num", "time_base_den", "source_seconds", "decoded_rgb_sha256")
        }
        sample = samples.setdefault(index, {**timing, "sample_index": index, "views": {}})
        if any(sample[k] != v for k, v in timing.items()):
            raise ValueError("Scoring views disagree about the original source frame")
        sample["views"][view] = json.loads(row["measurements_json"])
    ordered = [samples[k] for k in sorted(samples)]
    if (
        len(ordered) != segment["sample_count"]
        or any(len(s["views"]) != 2 for s in ordered)
        or any(b["source_seconds"] <= a["source_seconds"] for a, b in zip(ordered, ordered[1:], strict=False))
    ):
        raise ValueError("Scoring frames are incomplete or not increasing")
    for sample in ordered:
        sample["segment_id"] = segment["id"]
        sample["requested_interval_seconds"] = segment["request"]["interval_seconds"]
        sample["keyframes_only"] = segment["request"]["keyframes_only"]
        sample["evidence_id"] = segment["sample_anchors"].get(str(sample["sample_index"]))
    return ordered


def segments_in_session(session, source_id=None):
    rows = list(session.scalars(select(ScoringSegment).order_by(ScoringSegment.id)))
    selected = [r.document for r in rows if source_id is None or r.source_id == source_id]
    for segment in selected:
        for identifier in segment["anchor_evidence_ids"]:
            frame = session.get(Evidence, identifier)
            if frame is None or frame.source_id != segment["source_id"]:
                raise ValueError("Ice segment original evidence is missing")
    return selected


def ice_timeline(source, segments, samples):
    """Describe observed states; never label a stable mid-end score as final."""

    def anchor(sample):
        return {
            k: sample[k]
            for k in (
                "segment_id",
                "sample_index",
                "pts",
                "time_base_num",
                "time_base_den",
                "source_seconds",
                "decoded_rgb_sha256",
                "evidence_id",
            )
        } | {"youtube_url": video_link(source, sample["source_seconds"])}

    gaps = []
    for a, b in zip(samples, samples[1:], strict=False):
        allowance = max(a["requested_interval_seconds"], b["requested_interval_seconds"]) * 2 + 2
        if b["source_seconds"] - a["source_seconds"] > allowance:
            gaps.append({"after": anchor(a), "before": anchor(b)})
    views = {}
    for view in ("house_top", "house_bottom"):
        empty_runs, clearing, run, last_body, occupied_run = [], [], [], None, []
        for sample in samples:
            value = sample["views"][view]
            if run and sample["source_seconds"] - run[-1]["source_seconds"] > max(
                12, sample["requested_interval_seconds"] * 2 + 2
            ):
                if len(run) >= 3:
                    empty_runs.append(run)
                run = []
            if value.get("empty_house") is True:
                run.append(sample)
                if (
                    len(run) == 3
                    and last_body
                    and 0 < run[0]["source_seconds"] - last_body["source_seconds"] <= 180
                ):
                    before = last_body["views"][view]
                    clearing.append(
                        {
                            "view": view,
                            "last_visible_body": anchor(last_body),
                            "first_empty": anchor(run[0]),
                            "empty_confirmed": anchor(sample),
                            "last_visible_score": before.get("scoring", {}).get("provisional_score"),
                            "status": "inspection_bracket",
                            "final_end_state_verified": False,
                            "reason": "empty_house_can_also_occur_during_an_end",
                            "supporting_body_frames": [anchor(s) for s in occupied_run[-3:]],
                        }
                    )
            else:
                if len(run) >= 3:
                    empty_runs.append(run)
                run = []
                if value.get("footprints"):
                    last_body = sample
                    occupied_run.append(sample)
                    occupied_run = occupied_run[-3:]
        if len(run) >= 3:
            empty_runs.append(run)
        scores = [
            s
            for s in samples
            if s["views"][view].get("scoring", {}).get("provisional_score") is not None
            and not s["views"][view].get("empty_house")
        ]
        views[view] = {
            "empty_runs": [
                {"first": anchor(r[0]), "last": anchor(r[-1]), "sample_count": len(r)} for r in empty_runs
            ],
            "clearing_inspection_brackets": clearing,
            "nonempty_score_proposals": [
                {"frame": anchor(s), "score": s["views"][view]["scoring"]["provisional_score"]}
                for s in scores
            ],
            "unobservable_samples": sum(s["views"][view].get("status") != "observed" for s in samples),
        }
    document = {
        "version": "scoring-ice-timeline-v1",
        "source_id": source["id"],
        "segment_ids": [s["id"] for s in segments],
        "sample_count": len(samples),
        "first_seconds": samples[0]["source_seconds"] if samples else None,
        "last_seconds": samples[-1]["source_seconds"] if samples else None,
        "sampling_gaps": gaps,
        "views": views,
        "notice": "Discrete original keyframes locate inspection brackets. "
        "Clearing alone does not establish an end; pre-clearing scores and direction changes "
        "require native refinement and independent board reconciliation.",
    }
    return {**document, "sha256": digest(document)}


def source_ice_timeline(db, source_id):
    with db.snapshot_session() as session:
        source = source_or_error(session, source_id)
        segments = segments_in_session(session, source_id)
    if segments:
        latest = max(segments, key=lambda s: s["created_at"])["model_version"]
        segments = [
            s
            for s in segments
            if s["model_version"] == latest and s["source_hash"] == source["metadata_sha256"]
        ]
    by_time = {}
    for segment in segments:
        for sample in checked_samples(db.settings.data_dir, segment):
            time = sample["source_seconds"]
            if (
                time not in by_time
                or sample["requested_interval_seconds"] < by_time[time]["requested_interval_seconds"]
            ):
                by_time[time] = sample
    return ice_timeline(source, segments, [by_time[t] for t in sorted(by_time)])
