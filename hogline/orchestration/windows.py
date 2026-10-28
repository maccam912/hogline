"""Short dense windows share durable worker leases, with columnar observations."""

import hashlib
import math
import time
from collections import deque
from uuid import NAMESPACE_URL, uuid5

import numpy as np
from pydantic import Field, model_validator

from hogline.adapters.sources import digest, video_link
from hogline.analytics.dense import save_dense
from hogline.config import profile
from hogline.db import AnalysisWindow, Evidence, Job, now
from hogline.domain.catalog import source_or_error
from hogline.domain.models import Model
from hogline.media.frames import EvidenceBudget, observe, resolve_media, source_seconds
from hogline.media.windows import VERSION as DECODER_VERSION
from hogline.media.windows import decode_window
from hogline.orchestration.plans import check_execution, enqueue_in_session, finish_execution
from hogline.perception.pipeline import analyze_frame
from hogline.perception.stones import HSVHandleDetector
from hogline.perception.temporal import VERSION, ConfigurationTracker
from hogline.perception.visibility import foreground_regions


class WindowRequest(Model):
    source_id: str
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(gt=0)
    interval_seconds: float = Field(default=0.5, ge=0.25, le=2)
    max_frames: int = Field(default=120, ge=2, le=240)
    max_evidence_mb: int = Field(default=64, ge=1, le=256)
    max_worker_seconds: int = Field(default=120, ge=10, le=600)
    run_nonce: str | None = Field(default=None, min_length=1, max_length=80)

    @model_validator(mode="after")
    def bounded(self):
        duration = self.end_seconds - self.start_seconds
        if not 0 < duration <= 60 or math.ceil(duration / self.interval_seconds) > self.max_frames:
            raise ValueError("Detailed windows require 0–60 seconds within the declared frame budget")
        return self


def enqueue_window(db, request):
    from hogline.orchestration.worker import job_view

    with db.session() as session:
        source = source_or_error(session, request.source_id)
        if source.get("duration_seconds") and request.end_seconds > source["duration_seconds"]:
            raise ValueError("Detailed window exceeds source duration")
        layout = profile()
        return job_view(
            enqueue_in_session(
                session,
                {
                    "kind": "dense_window",
                    "request": request.model_dump(),
                    "source_hash": source["metadata_sha256"],
                    "profile": layout,
                    "profile_sha256": digest(layout),
                    "stage_version": VERSION,
                    "decoder_version": DECODER_VERSION,
                    "perception_version": HSVHandleDetector.version,
                },
            )
        )


def execute_window(db, job):
    from hogline.orchestration.worker import update_owned

    request = WindowRequest.model_validate(job["request"])
    with db.session() as session:
        source = source_or_error(session, request.source_id)
    if source["metadata_sha256"] != job["source_hash"]:
        raise ValueError("Source manifest changed; enqueue a current detailed window")
    if (job["stage_version"], job["decoder_version"], job["perception_version"]) != (
        VERSION,
        DECODER_VERSION,
        HSVHandleDetector.version,
    ):
        raise ValueError("Window processing version changed; enqueue current work")
    started, last_tick, token = time.monotonic(), 0, job["lease_token"]

    def tick():
        nonlocal last_tick
        elapsed = time.monotonic() - started
        if elapsed > request.max_worker_seconds:
            raise ValueError("Detailed window worker-time budget reached")
        if elapsed - last_tick >= 1:
            check_execution(db, token, elapsed)
            update_owned(db, job["id"], token, wall_seconds=elapsed)
            last_tick = elapsed

    handle = resolve_media(source)
    tick()
    layout, detector, tracker = job["profile"], HSVHandleDetector(), ConfigurationTracker()
    rows, previous, recent, retained = [], {}, deque(maxlen=8), {}
    first = last = None
    media_selection = {k: v for k, v in handle.items() if k != "uri"}
    with_decoder = decode_window(
        handle["uri"],
        request.start_seconds,
        request.end_seconds,
        interval=request.interval_seconds,
        max_frames=request.max_frames,
        timeout=min(120, max(1, request.max_worker_seconds - (time.monotonic() - started))),
        tick=tick,
    )
    try:
        for index, (image, timing) in enumerate(with_decoder):
            tick()
            seconds = source_seconds(timing, source)
            if not request.start_seconds - 0.001 <= seconds < request.end_seconds + 0.001:
                raise ValueError("Decoded timestamps fall outside the requested source window")
            frame = (index, image, timing)
            recent.append(frame)
            first, last = first or frame, frame
            rgb_sha = hashlib.sha256(image.tobytes()).hexdigest()
            for view in ("house_top", "house_bottom"):
                bounds = layout["views"][view]
                crop = image.crop(
                    tuple(
                        round(v * (image.width if n % 2 == 0 else image.height)) for n, v in enumerate(bounds)
                    )
                )
                vector = np.asarray(crop.convert("L").resize((96, 96)), dtype=np.float32)
                rgb = np.asarray(crop)
                row = {
                    "sample_index": index,
                    "source_id": source["id"],
                    "source_metadata_sha256": source["metadata_sha256"],
                    "profile_sha256": job["profile_sha256"],
                    "model_version": detector.version,
                    **timing,
                    "source_seconds": seconds,
                    "requested_interval_seconds": request.interval_seconds,
                    "decoded_rgb_sha256": rgb_sha,
                    "youtube_url": video_link(source, seconds),
                    "view": view,
                    "crop_width": crop.width,
                    "crop_height": crop.height,
                    "motion_mean_abs": float(np.abs(vector - previous[view]).mean())
                    if view in previous
                    else None,
                    "foreground_regions": foreground_regions(rgb),
                    **detector.inspect(rgb),
                }
                rows.append(row)
                previous[view] = vector
                if candidate := tracker.feed(row):
                    key = (view, candidate["sample_indices"][0])
                    if key in retained:
                        retained[key][1] = frame
                    elif len(retained) < 3:
                        beginning = next(f for f in recent if f[0] == key[1])
                        retained[key] = [beginning, frame]
            update_owned(
                db,
                job["id"],
                token,
                completed_frames=index + 1,
                progress=min(
                    0.9,
                    (seconds - request.start_seconds + request.interval_seconds)
                    / (request.end_seconds - request.start_seconds),
                ),
            )
    finally:
        with_decoder.close()
    if first is None:
        raise ValueError("No source frames were decoded")
    candidates = tracker.results()
    selected_candidates = [c for c in candidates if (c["view"], c["sample_indices"][0]) in retained]
    anchors = {f[0]: f for pair in retained.values() for f in pair}
    anchors[first[0]], anchors[last[0]] = first, last
    budget = EvidenceBudget(db.settings.data_dir, request.max_evidence_mb, db.settings.max_cache_mb)
    budget.reserve_growth = lambda size: check_execution(db, token, time.monotonic() - started, size)
    evidence, anchor_ids = {}, {}
    for index, image, timing in anchors.values():
        tick()
        doc, _ = observe(image, timing, source, db.settings.data_dir, budget, layout)
        doc["id"] = str(uuid5(NAMESPACE_URL, doc["evidence_key"]))
        doc["media_selection"] = {**media_selection, "decoded_frame_size": list(image.size)}
        evidence[doc["id"]] = doc
        anchor_ids[index] = doc["id"]
    for candidate in selected_candidates:
        candidate["evidence_ids"] = [anchor_ids[candidate["sample_indices"][n]] for n in (0, -1)]
        candidate["youtube_url"] = video_link(source, candidate["start_seconds"])
    artifact = save_dense(
        rows,
        {
            k: job[k]
            for k in (
                "request",
                "source_hash",
                "profile",
                "profile_sha256",
                "stage_version",
                "decoder_version",
                "perception_version",
            )
        },
        db.settings.data_dir,
        min(16 * 1024**2, request.max_evidence_mb * 1024**2),
        budget,
    )
    tick()
    result = {
        "id": str(uuid5(NAMESPACE_URL, f"dense-window:{job['id']}")),
        "created_at": now(),
        "source_id": source["id"],
        "source_metadata_sha256": source["metadata_sha256"],
        "request": request.model_dump(),
        "profile_sha256": job["profile_sha256"],
        "version": VERSION,
        "decoder_version": DECODER_VERSION,
        "model_version": detector.version,
        "sample_count": len(rows) // 2,
        "observed_first_seconds": rows[0]["source_seconds"],
        "observed_last_seconds": rows[-1]["source_seconds"],
        "coverage_kind": "discrete_dense_samples",
        "parquet": artifact,
        "anchor_evidence_ids": sorted(evidence),
        "candidates": selected_candidates,
        "deferred_candidate_count": len(candidates) - len(selected_candidates),
        "diagnostics": {
            view: {
                "samples": sum(r["view"] == view for r in rows),
                "no_handle_candidates": sum(r["view"] == view and not r["detections"] for r in rows),
                "large_dark_regions": sum(r["view"] == view and bool(r["foreground_regions"]) for r in rows),
                "clipped_handle_candidates": sum(
                    r["view"] == view and any(d["boundary_clipped"] for d in r["detections"]) for r in rows
                ),
                "rejected_color_candidates": sum(
                    len(r["rejected_candidates"]) for r in rows if r["view"] == view
                ),
                "high_motion": sum(r["view"] == view and (r["motion_mean_abs"] or 0) > 2.5 for r in rows),
            }
            for view in ("house_top", "house_bottom")
        },
        "acceptance": "proposed",
        "score": None,
        "wall_seconds": time.monotonic() - started,
        "evidence_bytes": budget.logical_bytes,
        "media_selection": media_selection,
    }
    check_execution(db, token, time.monotonic() - started)
    with db.session() as session:
        row = session.get(Job, job["id"])
        if row.state != "running" or row.document.get("lease_token") != token:
            raise ValueError("Work lease expired before window publication")
        if source_or_error(session, source["id"])["metadata_sha256"] != source["metadata_sha256"]:
            raise ValueError("Source manifest changed before window publication")
        for doc in evidence.values():
            if session.get(Evidence, doc["id"]) is None:
                session.add(
                    Evidence(
                        id=doc["id"], source_id=source["id"], evidence_key=doc["evidence_key"], document=doc
                    )
                )
        session.flush()
        for evidence_id in evidence:
            analyze_frame(session, session.get(Evidence, evidence_id), db.settings.data_dir)
        session.add(AnalysisWindow(id=result["id"], job_id=row.id, source_id=source["id"], document=result))
        row.state, row.lease_until = "succeeded", None
        row.document = {
            **row.document,
            "finished_at": now(),
            "progress": 1,
            "wall_seconds": result["wall_seconds"],
            "evidence_bytes": budget.logical_bytes,
        }
        finish_execution(session, token, "succeeded", time.monotonic() - started)
    return result
