"""Whole-recording ice surveys and bounded pre-clearing refinements."""

import hashlib
import json
import math
import time
import zlib
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import numpy as np
import pyarrow as pa
from PIL import Image
from sqlalchemy import select

from hogline.adapters.sources import digest
from hogline.analytics.dense import save_dense
from hogline.config import profile
from hogline.db import Evidence, Job, ProcessingBatch, ScoringSegment, now
from hogline.domain.catalog import source_or_error
from hogline.domain.provenance import observation_manifest, resolve_observation_manifests
from hogline.media.frames import EvidenceBudget, observe, resolve_media, source_seconds
from hogline.media.windows import SCORING_VERSION, decode_scoring_segment
from hogline.orchestration.plans import (
    WorkStopped,
    check_execution,
    check_execution_in_session,
    finish_execution,
    publish_batch,
)
from hogline.orchestration.scorecards import checked_image
from hogline.perception.ice_score import VERSION, IceInspector

KIND = "ice_survey"
SCHEMA_VERSION = "hogline-scoring-samples-v1"
SCHEMA = pa.schema(
    [
        ("sample_index", pa.int32()),
        ("pts", pa.int64()),
        ("time_base_num", pa.int32()),
        ("time_base_den", pa.int32()),
        ("source_seconds", pa.float64()),
        ("decoded_rgb_sha256", pa.string()),
        ("view", pa.string()),
        ("measurements_json", pa.string()),
    ]
)


def crop(image, bounds):
    return image.crop(
        tuple(round(v * (image.width if i % 2 == 0 else image.height)) for i, v in enumerate(bounds))
    )


def checked_reference(session, root, reference, source, layout):
    if digest({k: v for k, v in reference.items() if k != "sha256"}) != reference.get("sha256"):
        raise ValueError("Empty-house reference checksum mismatch")
    frame = session.get(Evidence, reference["evidence_id"])
    manifest = None
    if frame is not None:
        manifest = observation_manifest(
            resolve_observation_manifests(
                session,
                [
                    {
                        "id": frame.id,
                        "evidence_id": frame.id,
                        "source_id": frame.source_id,
                    }
                ],
            )[0]
        )
    if (
        frame is None
        or frame.source_id != source["id"]
        or reference.get("source_id") != source["id"]
        or reference.get("source_metadata_sha256") != source["metadata_sha256"]
        or manifest != source["metadata_sha256"]
        or digest(frame.document) != reference.get("evidence_document_sha256")
        or reference["image"] != frame.document["image"]
        or reference["bounds"] != layout["views"].get(reference["view"])
        or reference["geometry_profile_sha256"] != digest(layout)
    ):
        raise ValueError("Empty-house reference differs from source, evidence or geometry")
    image = crop(checked_image(root, reference["image"]), reference["bounds"])
    if (
        list(image.size) != reference["crop_size"]
        or hashlib.sha256(image.tobytes()).hexdigest() != reference["crop_rgb_sha256"]
    ):
        raise ValueError("Empty-house reference pixels changed")
    return IceInspector(np.asarray(image), reference)


def create_ice_batch(
    db, source_id, *, references_path=None, start=0, end=None, interval=5, keyframes_only=True
):
    references_path = Path(references_path or db.settings.data_dir / "scoring/empty-house-references.json")
    references = json.loads(references_path.read_text())["references"]
    layout = profile()
    with db.snapshot_session(write=True) as session:
        source = source_or_error(session, source_id)
        end = source.get("duration_seconds") if end is None else end
        if (
            not isinstance(end, (float, int))
            or not all(math.isfinite(x) for x in (start, end, interval))
            or not 0 <= start < end <= source["duration_seconds"]
            or not 0.5 <= interval <= 10
            or not isinstance(keyframes_only, bool)
        ):
            raise ValueError("Ice coverage needs finite source bounds and a 0.5–10-second interval")
        refs = [r for r in references if r["source_id"] == source_id]
        if sorted(r["view"] for r in refs) != ["house_bottom", "house_top"]:
            raise ValueError("Both houses need one reviewed empty reference")
        for r in refs:
            checked_reference(session, db.settings.data_dir, r, source, layout)
        version = digest(
            {"method": VERSION, "references": refs, "profile": layout, "decoder": SCORING_VERSION}
        )
        request = {
            "source_ids": [source_id],
            "start_seconds": start,
            "end_seconds": end,
            "interval_seconds": interval,
            "keyframes_only": keyframes_only,
            "max_worker_seconds": 86400,
            "max_image_growth_mb": 4096,
        }
        for row in session.scalars(
            select(ProcessingBatch).where(ProcessingBatch.state.in_(["active", "paused"]))
        ):
            if row.document.get("kind") == KIND and row.document.get("request") == request:
                from hogline.orchestration.plans import batch_view

                return batch_view(session, row)
        units = []
        # Ten-minute jobs provide checkpoints and cancellation without thousands of tiny HTTP seeks.
        for first in np.arange(start, end, 600):
            last = min(end, float(first) + 600)
            payload = {
                "kind": KIND,
                "stage_version": VERSION,
                "decoder_version": SCORING_VERSION,
                "model_version": version,
                "source_hash": source["metadata_sha256"],
                "profile": layout,
                "profile_sha256": digest(layout),
                "references": refs,
                "request": {
                    "source_id": source_id,
                    "start_seconds": float(first),
                    "end_seconds": last,
                    "interval_seconds": interval,
                    "keyframes_only": keyframes_only,
                    "max_frames": math.ceil((last - first) / interval),
                    "max_worker_seconds": 600,
                    "max_evidence_mb": 64,
                },
            }
            matching = session.scalar(select(Job).where(Job.job_key == digest(payload)))
            disposition = (
                "reuse_succeeded"
                if matching and matching.state == "succeeded"
                else "existing_needs_attention"
                if matching
                else "new"
            )
            units.append({"payload": payload, "disposition": disposition})
        plan = {
            "kind": KIND,
            "phase": "ice",
            "request": request,
            "units": units,
            "coverage_kind": "source_grid_keyframe_survey"
            if keyframes_only
            else "source_grid_native_refinement",
            "created_at": now(),
        }
        plan["sha256"] = digest(plan)
        return publish_batch(session, plan)


def execute_ice(db, job):
    from hogline.orchestration.worker import update_owned

    if job["stage_version"] != VERSION or job["decoder_version"] != SCORING_VERSION:
        raise ValueError("Ice methods changed; schedule current work")
    started, token, request = time.monotonic(), job["lease_token"], job["request"]
    last_tick = 0

    def tick():
        nonlocal last_tick
        elapsed = time.monotonic() - started
        if elapsed > request["max_worker_seconds"]:
            raise ValueError("Ice worker-time allowance reached")
        if elapsed - last_tick >= 1:
            check_execution(db, token, elapsed)
            update_owned(db, job["id"], token, wall_seconds=elapsed)
            last_tick = elapsed

    with db.session() as session:
        source = source_or_error(session, request["source_id"])
        if source["metadata_sha256"] != job["source_hash"]:
            raise ValueError("Ice source changed after scheduling")
        inspectors = {
            r["view"]: checked_reference(session, db.settings.data_dir, r, source, job["profile"])
            for r in job["references"]
        }
    check_execution(db, token, 0)
    handle = resolve_media(source)
    rows, retained, empty_runs, previous = [], {}, {}, {}

    # Keep only selected losslessly compressed originals in working memory.
    def pack(index, image, timing):
        return (index, image.size, zlib.compress(image.tobytes(), 1), timing)

    def keep(frame):
        if len(retained) < 12 or frame[0] in retained:
            retained[frame[0]] = frame

    decoder = decode_scoring_segment(
        handle["uri"],
        request["start_seconds"],
        request["end_seconds"],
        interval=request["interval_seconds"],
        max_frames=request["max_frames"],
        timeout=request["max_worker_seconds"],
        tick=tick,
        source_time_origin=-source_seconds({"pts": 0, "time_base_num": 1, "time_base_den": 1}, source),
        keyframes_only=request["keyframes_only"],
    )
    first = last = None
    try:
        for index, (image, timing) in enumerate(decoder):
            tick()
            seconds = source_seconds(timing, source)
            if not request["start_seconds"] - 0.001 <= seconds < request["end_seconds"] + 0.001:
                raise ValueError("Ice decoder timestamp outside source interval")
            frame = pack(index, image, timing)
            first, last = first or frame, frame
            rgb_sha = hashlib.sha256(image.tobytes()).hexdigest()
            for view, inspector in inspectors.items():
                measurements = inspector.inspect(np.asarray(crop(image, job["profile"]["views"][view])))
                rows.append(
                    {
                        "sample_index": index,
                        **timing,
                        "source_seconds": seconds,
                        "decoded_rgb_sha256": rgb_sha,
                        "view": view,
                        "measurements_json": json.dumps(measurements, sort_keys=True, separators=(",", ":")),
                    }
                )
                # Retain the approaches to repeated empty matches for later native refinement.
                if measurements.get("empty_house") is True:
                    run = empty_runs.setdefault(view, [])
                    if len(run) < 3:
                        run.append(frame)
                        if len(run) == 3 and view in previous:
                            keep(previous[view])
                            keep(run[0])
                            keep(frame)
                else:
                    empty_runs[view] = []
                    previous[view] = frame
            update_owned(
                db,
                job["id"],
                token,
                completed_frames=index + 1,
                progress=(seconds - request["start_seconds"])
                / (request["end_seconds"] - request["start_seconds"]),
            )
    finally:
        decoder.close()
    if first is None:
        raise ValueError("Ice decoder returned no source samples")
    retained[first[0]], retained[last[0]] = first, last
    budget = EvidenceBudget(db.settings.data_dir, request["max_evidence_mb"], db.settings.max_cache_mb)
    budget.reserve_growth = lambda size: check_execution(db, token, time.monotonic() - started, size)
    evidence, anchor_ids = {}, {}
    for index, size, content, timing in retained.values():
        tick()
        image = Image.frombytes("RGB", size, zlib.decompress(content))
        document, _ = observe(image, timing, source, db.settings.data_dir, budget, job["profile"])
        identifier = str(uuid5(NAMESPACE_URL, document["evidence_key"]))
        document = {
            **document,
            "id": identifier,
            "media_selection": {k: v for k, v in handle.items() if k != "uri"},
        }
        evidence[identifier], anchor_ids[index] = document, identifier
    manifest = {
        k: job[k]
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
    artifact = save_dense(
        rows,
        manifest,
        db.settings.data_dir,
        16 * 1024**2,
        budget,
        schema=SCHEMA,
        schema_version=SCHEMA_VERSION,
    )
    result = {
        "id": str(uuid5(NAMESPACE_URL, f"ice:{job['id']}")),
        "source_id": source["id"],
        "source_metadata_sha256": source["metadata_sha256"],
        **manifest,
        "parquet": artifact,
        "sample_count": len(rows) // 2,
        "first_seconds": rows[0]["source_seconds"],
        "last_seconds": rows[-1]["source_seconds"],
        "anchor_evidence_ids": sorted(evidence),
        "sample_anchors": {str(k): v for k, v in anchor_ids.items()},
        "coverage_kind": "source_grid_keyframe_survey"
        if request["keyframes_only"]
        else "source_grid_native_refinement",
        "limitations": [
            "discrete_samples_can_miss_short_events",
            "stable_score_is_not_final_end_proof",
            "background_references_are_development_calibration",
        ],
        "created_at": now(),
    }
    result["sha256"] = digest(result)
    with db.snapshot_session(write=True) as session:
        if stopped := check_execution_in_session(session, token, time.monotonic() - started):
            raise WorkStopped(stopped)
        if source_or_error(session, source["id"])["metadata_sha256"] != job["source_hash"]:
            raise ValueError("Ice source changed before publication")
        for identifier, document in evidence.items():
            if session.get(Evidence, identifier) is None:
                session.add(
                    Evidence(
                        id=identifier,
                        evidence_key=document["evidence_key"],
                        source_id=source["id"],
                        document=document,
                    )
                )
        session.add(
            ScoringSegment(id=result["id"], source_id=source["id"], job_id=job["id"], document=result)
        )
        row = session.get(Job, job["id"])
        row.state, row.lease_until = "succeeded", None
        row.document = {
            **row.document,
            "finished_at": now(),
            "progress": 1,
            "wall_seconds": time.monotonic() - started,
            "evidence_bytes": budget.logical_bytes,
        }
        finish_execution(session, token, "succeeded", time.monotonic() - started)
    return {"id": result["id"], "sample_count": result["sample_count"]}
