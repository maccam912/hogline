"""Game lineups: operator-requested face observations grouped into anonymous game players.

A lineup batch decodes both end-camera views across one game span in bounded segments.
Each segment keeps quality-gated face templates and small aligned thumbnails in the local
biometric store. When every segment has succeeded, an assembly job groups the faces.
"""

import math
import time
from typing import Literal
from uuid import NAMESPACE_URL, uuid4, uuid5

import numpy as np
from pydantic import Field, model_validator
from sqlalchemy import select

from hogline.adapters.sources import digest, video_link
from hogline.analytics.biometrics import array_json, delete_archive, json_array, read_archive, write_archive
from hogline.config import profile
from hogline.db import GameLineup, Job, LineupGrouping, LineupLink, LineupSegment, ProcessingBatch, now
from hogline.domain.catalog import source_or_error
from hogline.domain.models import Model
from hogline.media.frames import resolve_media, source_seconds
from hogline.media.post_live import has_verified_download
from hogline.media.windows import SCORING_VERSION, decode_scoring_segment
from hogline.orchestration.plans import (
    WorkStopped,
    check_execution,
    check_execution_in_session,
    enqueue_in_session,
    finish_execution,
    publish_batch,
)
from hogline.perception.faces import THUMB_SIZE, installed_face_version, optional_face_models
from hogline.perception.faces import VERSION as FACE_VERSION
from hogline.perception.people import MODEL_SHA256 as PERSON_MODEL_SHA256
from hogline.perception.people import VERSION as PERSON_VERSION
from hogline.perception.people import optional_person_detector
from hogline.reconstruction.lineups import (
    DEFAULTS,
    PORTRAIT_VERSION,
    THUMBNAIL_POLICY,
    classify_players,
    face_quality,
    group_faces,
    portrait_face,
    representative_faces,
    select_thumbnails,
    template,
)
from hogline.reconstruction.lineups import VERSION as GROUPING_VERSION

KIND = "lineup"
SEGMENT_KIND = "lineup_segment"
ASSEMBLY_KIND = "lineup_assembly"
VIEWS = ("perspective_left", "perspective_right")
MAX_THUMBNAILS = 240
UNFINISHED_RECORDING = (
    "This recording's length is unknown or YouTube is still processing it (for example a stream that "
    "was live when it was added). Refresh its metadata once YouTube has finished, then try again"
)


class LineupRequest(Model):
    source_id: str
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(gt=0)
    interval_seconds: float = Field(default=1.0, ge=0.5, le=10)
    segment_seconds: float = Field(default=300, ge=30, le=600)
    min_eye_px: float = Field(default=6.0, ge=3, le=40)
    min_foot_y: float = Field(default=0.4, ge=0, le=1)
    embedder: Literal["arcface-r50", "sface"] | None = None
    parallelism: int = Field(default=3, ge=1, le=8)
    max_worker_seconds: int = Field(default=14400, ge=60, le=86400)
    max_image_growth_mb: int = Field(default=1024, ge=16, le=8192)
    # Small aligned face images kept per segment, chosen across the people in it.
    max_thumbnails: int = Field(default=MAX_THUMBNAILS, ge=16, le=MAX_THUMBNAILS)

    @model_validator(mode="after")
    def bounded(self):
        duration = self.end_seconds - self.start_seconds
        if not 0 < duration <= 6 * 3600:
            raise ValueError("A lineup covers one game: 0–6 hours of one recording")
        if math.ceil(duration / self.segment_seconds) > 200:
            raise ValueError("Too many lineup segments; lengthen segments or shorten the span")
        return self


def segment_bounds(request):
    """Segments follow a source grid so overlapping lineups can reuse identical work."""
    size = request.segment_seconds
    first = math.floor(request.start_seconds / size) * size
    bounds = []
    while first < request.end_seconds:
        start, end = max(request.start_seconds, first), min(request.end_seconds, first + size)
        if end - start >= request.interval_seconds:
            bounds.append((float(start), float(end)))
        first += size
    if not bounds:
        raise ValueError("Lineup span contains no sample time")
    return bounds


def lineup_models(root, embedder=None):
    """Check the pinned face and person models before any work is planned."""
    embedder, face_version = installed_face_version(root, embedder)
    detector, info = optional_person_detector(root)
    if detector is None:
        raise ValueError(f"Lineups need the pinned person model ({info['missing_reason']})")
    return embedder, face_version


def create_lineup_batch(db, request: LineupRequest):
    embedder, face_version = lineup_models(db.settings.data_dir, request.embedder)
    with db.snapshot_session(write=True) as session:
        return lineup_batch_in_session(session, request, embedder, face_version)


def lineup_batch_in_session(
    session, request: LineupRequest, embedder, face_version, automatic=None, *, rerun_nonce=None
):
    layout = profile()
    source = source_or_error(session, request.source_id)
    if not source.get("duration_seconds") or (
        source.get("live_status") in ("is_live", "is_upcoming")
        or (source.get("live_status") == "post_live" and not has_verified_download(source))
    ):
        raise ValueError(UNFINISHED_RECORDING)
    if request.end_seconds > source["duration_seconds"] + 1:
        raise ValueError("Lineup span exceeds the recording duration")
    lineup = {**request.model_dump(), "embedder": embedder, "face_version": face_version}
    if automatic:
        lineup["automatic"] = automatic
    units = []
    for start, end in segment_bounds(request):
        payload = {
            "kind": SEGMENT_KIND,
            "stage_version": FACE_VERSION,
            "face_version": face_version,
            "embedder": embedder,
            "decoder_version": SCORING_VERSION,
            "person_version": PERSON_VERSION,
            "person_model_sha256": PERSON_MODEL_SHA256,
            "source_hash": source["metadata_sha256"],
            "profile": layout,
            "profile_sha256": digest(layout),
            "request": {
                "source_id": source["id"],
                "start_seconds": start,
                "end_seconds": end,
                "interval_seconds": request.interval_seconds,
                "max_frames": math.ceil((end - start) / request.interval_seconds),
                "max_worker_seconds": 900,
                "min_eye_px": request.min_eye_px,
                "min_score": 0.7,
                "min_foot_y": request.min_foot_y,
                "thumbnails": {"policy": THUMBNAIL_POLICY, "max": request.max_thumbnails},
            },
        }
        if rerun_nonce:
            payload["rerun_nonce"] = rerun_nonce
        matching = session.scalar(select(Job).where(Job.job_key == digest(payload)))
        if matching and matching.state == "succeeded":
            if session.scalar(select(LineupSegment).where(LineupSegment.job_id == matching.id)) is None:
                # Its biometric output was deleted; observe the interval again.
                payload["rerun_nonce"] = str(uuid4())
                matching = None
        disposition = (
            "reuse_succeeded"
            if matching and matching.state == "succeeded"
            else "existing_needs_attention"
            if matching and matching.state in ("queued", "running")
            else "new"
        )
        if matching and matching.state in ("failed", "cancelled"):
            payload["rerun_nonce"] = str(uuid4())
        units.append({"payload": payload, "disposition": disposition})
    plan = {
        "kind": KIND,
        "phase": "lineup",
        "parallelism": request.parallelism,
        "lineup": lineup,
        "request": {
            "source_ids": [source["id"]],
            "start_seconds": request.start_seconds,
            "end_seconds": request.end_seconds,
            "max_worker_seconds": request.max_worker_seconds,
            "max_image_growth_mb": request.max_image_growth_mb,
        },
        "units": units,
        "deferred_units": 0,
        "coverage_kind": "source_grid_face_samples",
        "created_at": now(),
        **({"automatic": automatic} if automatic else {}),
    }
    plan["sha256"] = digest(plan)
    view = publish_batch(session, plan)
    if view["state"] == "completed":
        enqueue_assembly(session, session.get(ProcessingBatch, view["id"]))
    return view


def enqueue_assembly(session, batch, params=None):
    payload = {
        "kind": ASSEMBLY_KIND,
        # The job queue owns "batch_id"; assembly runs outside the segment batch.
        "lineup_batch_id": batch.id,
        "segment_job_ids": [u["job_id"] for u in batch.document["units"]],
        "lineup": batch.document["lineup"],
        # Job lists expect a source span on every job; grouping covers the whole lineup.
        "request": {
            "source_id": batch.document["lineup"]["source_id"],
            "start_seconds": batch.document["lineup"]["start_seconds"],
            "end_seconds": batch.document["lineup"]["end_seconds"],
        },
        "grouping_version": GROUPING_VERSION,
        "params": {**DEFAULTS, **(params or {})},
    }
    return enqueue_in_session(session, payload)


def execute_lineup_segment(db, job):
    from hogline.orchestration.worker import update_owned

    if (job["stage_version"], job["decoder_version"], job["person_version"], job["person_model_sha256"]) != (
        FACE_VERSION,
        SCORING_VERSION,
        PERSON_VERSION,
        PERSON_MODEL_SHA256,
    ):
        raise ValueError("Lineup methods changed; create a current lineup")
    started, token, request = time.monotonic(), job["lease_token"], job["request"]
    last_tick = 0

    def tick():
        nonlocal last_tick
        elapsed = time.monotonic() - started
        if elapsed > request["max_worker_seconds"]:
            raise ValueError("Lineup segment worker-time allowance reached")
        if elapsed - last_tick >= 1:
            check_execution(db, token, elapsed)
            update_owned(db, job["id"], token, wall_seconds=elapsed)
            last_tick = elapsed

    with db.session() as session:
        source = source_or_error(session, request["source_id"])
    if source["metadata_sha256"] != job["source_hash"]:
        raise ValueError("Lineup source changed after scheduling")
    people, people_info = optional_person_detector(db.settings.data_dir)
    if people is None:
        raise ValueError(f"Person model unavailable: {people_info['missing_reason']}")
    faces_model, face_info = optional_face_models(db.settings.data_dir, job["embedder"])
    if faces_model is None or faces_model.version != job["face_version"]:
        raise ValueError(f"Face model unavailable or changed: {face_info.get('missing_reason', 'version')}")
    check_execution(db, token, 0)
    handle = resolve_media(source)
    layout = job["profile"]
    meta, vectors, thumbs, totals = [], [], [], {}
    samples = 0
    decoder = decode_scoring_segment(
        handle["uri"],
        request["start_seconds"],
        request["end_seconds"],
        interval=request["interval_seconds"],
        max_frames=request["max_frames"],
        timeout=min(600, request["max_worker_seconds"]),
        tick=tick,
        source_time_origin=-source_seconds({"pts": 0, "time_base_num": 1, "time_base_den": 1}, source),
        keyframes_only=False,
    )
    try:
        for index, (image, timing) in enumerate(decoder):
            tick()
            seconds = source_seconds(timing, source)
            if not request["start_seconds"] - 0.001 <= seconds < request["end_seconds"] + 0.001:
                raise ValueError("Lineup decoder timestamp outside the requested interval")
            samples += 1
            for view in VIEWS:
                bounds = layout["views"][view]
                crop = image.crop(
                    tuple(
                        round(v * (image.width if i % 2 == 0 else image.height)) for i, v in enumerate(bounds)
                    )
                )
                rgb = np.asarray(crop)
                found, counts = faces_model.observe(
                    rgb,
                    people.detect(rgb),
                    min_eye_px=request["min_eye_px"],
                    min_score=request["min_score"],
                    min_foot_y=request["min_foot_y"],
                )
                for key, value in counts.items():
                    totals[key] = totals.get(key, 0) + value
                for face in found:
                    vectors.append(face.pop("embedding"))
                    thumbs.append(face.pop("thumbnail"))
                    meta.append(
                        {
                            **face,
                            "sample_index": index,
                            "view": view,
                            **timing,
                            "source_seconds": seconds,
                            "crop_width": crop.width,
                            "crop_height": crop.height,
                        }
                    )
            update_owned(
                db,
                job["id"],
                token,
                completed_frames=index + 1,
                progress=min(
                    0.95,
                    (seconds - request["start_seconds"])
                    / (request["end_seconds"] - request["start_seconds"]),
                ),
            )
    finally:
        decoder.close()
    if not samples:
        raise ValueError("Lineup decoder returned no source samples")
    policy = request.get("thumbnails")
    if policy:
        if policy["policy"] != THUMBNAIL_POLICY:
            raise ValueError("Lineup thumbnail selection changed; create a current lineup")
        keep = select_thumbnails(meta, vectors, policy["max"])
    else:  # Lineups planned before thumbnail selection kept the largest faces.
        keep = sorted(range(len(meta)), key=lambda i: (-meta[i]["eye_px"], i))[:MAX_THUMBNAILS]
    slots = {face: slot for slot, face in enumerate(sorted(keep))}
    for i, face in enumerate(meta):
        face["thumb"] = slots.get(i, -1)
        face["index"] = i
    segment_id = str(uuid5(NAMESPACE_URL, f"lineup-segment:{job['id']}"))
    arrays = {
        "meta": json_array(meta),
        "embeddings": np.asarray(vectors, dtype=np.float16).reshape(len(meta), faces_model.dimensions),
        "thumbnails": np.asarray([thumbs[i] for i in sorted(keep)], dtype=np.uint8).reshape(
            len(keep), THUMB_SIZE, THUMB_SIZE, 3
        ),
    }
    archive = write_archive(
        db.settings.data_dir,
        f"biometrics/lineup-segments/{segment_id}.npz",
        arrays,
        reserve=lambda size: check_execution(db, token, time.monotonic() - started, size),
    )
    document = {
        "id": segment_id,
        "job_id": job["id"],
        "source_id": source["id"],
        "source_metadata_sha256": source["metadata_sha256"],
        "request": request,
        "face_version": job["face_version"],
        "embedder": job["embedder"],
        "dimensions": faces_model.dimensions,
        "decoder_version": SCORING_VERSION,
        "person_version": PERSON_VERSION,
        "profile_sha256": job["profile_sha256"],
        "sample_count": samples,
        "face_count": len(meta),
        "thumbnail_count": len(keep),
        "counts": totals,
        "archive": archive,
        "media_selection": {k: v for k, v in handle.items() if k != "uri"},
        "coverage_kind": "source_grid_face_samples",
        "limitations": [
            "discrete_samples_miss_faces_between_samples",
            "sheet_membership_unresolved_adjacent_sheets_can_appear",
            "spectator_exclusion_uses_seed_foot_height",
        ],
        "created_at": now(),
    }
    with db.snapshot_session(write=True) as session:
        if stopped := check_execution_in_session(session, token, time.monotonic() - started):
            raise WorkStopped(stopped)
        if source_or_error(session, source["id"])["metadata_sha256"] != job["source_hash"]:
            raise ValueError("Lineup source changed before publication")
        session.add(
            LineupSegment(
                id=segment_id,
                job_id=job["id"],
                source_id=source["id"],
                sha256=digest(document),
                document=document,
            )
        )
        row = session.get(Job, job["id"])
        row.state, row.lease_until = "succeeded", None
        row.document = {
            **row.document,
            "finished_at": now(),
            "progress": 1,
            "wall_seconds": time.monotonic() - started,
            "faces": len(meta),
        }
        finish_execution(session, token, "succeeded", time.monotonic() - started)
        batch = (
            session.get(ProcessingBatch, row.document["batch_id"]) if row.document.get("batch_id") else None
        )
        if batch is not None and batch.document.get("kind") == KIND and batch.state == "completed":
            enqueue_assembly(session, batch)
    return {"id": segment_id, "faces": len(meta), "samples": samples}


def checked_segment(row):
    if digest(row.document) != row.sha256 or row.document["id"] != row.id:
        raise ValueError("Lineup segment checksum mismatch")
    return row.document


def load_faces(data_dir, segments):
    """Concatenate segment faces in stable time order, verifying each archive."""
    metas, vectors, thumbnails = [], [], {}
    for segment in segments:
        arrays = read_archive(data_dir, segment["archive"])
        meta = array_json(arrays["meta"])
        if len(meta) != len(arrays["embeddings"]):
            raise ValueError("Lineup segment archive is inconsistent")
        thumbnails[segment["id"]] = arrays["thumbnails"]
        for face, vector in zip(meta, arrays["embeddings"], strict=True):
            metas.append({**face, "segment_id": segment["id"]})
            vectors.append(vector)
    order = sorted(
        range(len(metas)), key=lambda i: (metas[i]["source_seconds"], metas[i]["view"], metas[i]["index"])
    )
    metas = [metas[i] for i in order]
    vectors = np.asarray([vectors[i] for i in order], dtype=np.float32).reshape(len(order), -1)
    if len(vectors):
        vectors /= np.maximum(np.linalg.norm(vectors, axis=1, keepdims=True), 1e-12)
    return metas, vectors, thumbnails


def execute_lineup_assembly(db, job):
    if job["grouping_version"] != GROUPING_VERSION:
        raise ValueError("Lineup grouping changed; regroup with the current method")
    started, token = time.monotonic(), job["lease_token"]
    with db.snapshot_session() as session:
        segments = []
        for job_id in job["segment_job_ids"]:
            row = session.scalar(select(LineupSegment).where(LineupSegment.job_id == job_id))
            if row is None:
                raise ValueError("A lineup segment is missing or was deleted; create the lineup again")
            segments.append(checked_segment(row))
        source = source_or_error(session, segments[0]["source_id"])
    if {s["face_version"] for s in segments} != {job["lineup"]["face_version"]}:
        raise ValueError("Lineup segments used different face models")
    meta, vectors, thumbnails = load_faces(db.settings.data_dir, segments)

    def thumbnail(i):
        return thumbnails[meta[i]["segment_id"]][meta[i]["thumb"]]

    check_execution(db, token, time.monotonic() - started)
    params = job["params"]
    labels, diagnostics = group_faces(vectors, meta, params)
    lineup_id = str(uuid5(NAMESPACE_URL, f"lineup:{job['id']}"))
    players, templates = [], []
    for number in range(int(labels.max()) + 1 if len(labels) else 0):
        members = [i for i in range(len(meta)) if labels[i] == number]
        if not members:
            continue
        times = [meta[i]["source_seconds"] for i in members]
        weights = [meta[i]["eye_px"] for i in members]
        best = max(members, key=lambda i: (meta[i]["eye_px"], -i))
        span = max(times) - min(times)
        group_template = template(vectors[members], weights)
        chosen = portrait_face(members, meta, vectors, group_template, thumbnail)
        player = {
            "id": str(uuid5(NAMESPACE_URL, f"lineup-player:{lineup_id}:{number}")),
            "number": number + 1,
            "faces": len(members),
            "first_seconds": min(times),
            "last_seconds": max(times),
            "span_seconds": span,
            "views": {v: sum(meta[i]["view"] == v for i in members) for v in VIEWS},
            "median_eye_px": float(np.median(weights)),
            "median_foot_x": float(np.median([meta[i]["foot_x"] for i in members])),
            "best_face": {
                "segment_id": meta[best]["segment_id"],
                "index": meta[best]["index"],
                "source_seconds": meta[best]["source_seconds"],
                "youtube_url": video_link(source, meta[best]["source_seconds"]),
            },
            "thumbnails": [
                {
                    "segment_id": meta[i]["segment_id"],
                    "thumb": meta[i]["thumb"],
                    "source_seconds": meta[i]["source_seconds"],
                }
                for i in representative_faces(members, meta)
            ],
            "portrait": None
            if chosen is None
            else {
                "segment_id": meta[chosen[0]]["segment_id"],
                "thumb": meta[chosen[0]]["thumb"],
                "source_seconds": meta[chosen[0]]["source_seconds"],
                "view": meta[chosen[0]]["view"],
                "eye_px": meta[chosen[0]]["eye_px"],
                "quality": round(face_quality(meta[chosen[0]]), 3),
                "template_similarity": round(chosen[1], 4),
                "youtube_url": video_link(source, meta[chosen[0]]["source_seconds"]),
            },
        }
        players.append(player)
        templates.append(group_template)
    request = job["lineup"]
    diagnostics["roles"] = classify_players(
        players, request["end_seconds"] - request["start_seconds"], params
    )
    archive = write_archive(
        db.settings.data_dir,
        f"biometrics/lineups/{lineup_id}.npz",
        {
            "player_ids": json_array([p["id"] for p in players]),
            "templates": np.asarray(templates, dtype=np.float32).reshape(
                len(players), vectors.shape[1] if len(vectors) else 0
            ),
        },
    )
    document = {
        "id": lineup_id,
        "batch_id": job["lineup_batch_id"],
        "source_id": source["id"],
        "source_metadata_sha256": source["metadata_sha256"],
        "title": source.get("title"),
        "sheet": source.get("sheet"),
        "title_date": source.get("title_date"),
        "start_seconds": request["start_seconds"],
        "end_seconds": request["end_seconds"],
        "request": request,
        "embedder": request["embedder"],
        "face_version": request["face_version"],
        "grouping_version": GROUPING_VERSION,
        "portrait_version": PORTRAIT_VERSION,
        "params": params,
        "segments": [
            {"id": s["id"], "sha256": digest(s), "sample_count": s["sample_count"]} for s in segments
        ],
        "sample_count": sum(s["sample_count"] for s in segments),
        "face_count": len(meta),
        "players": players,
        "diagnostics": diagnostics,
        "templates": archive,
        "acceptance": "proposed",
        "limitations": [
            "one_person_can_be_split_into_several_groups",
            "look_alikes_or_matching_caps_can_merge",
            "adjacent_sheet_players_can_appear",
            "names_require_operator_confirmation",
        ],
        "created_at": now(),
    }
    try:
        publish_lineup(db, job, token, started, document)
    except Exception:
        delete_archive(db.settings.data_dir, archive)
        raise
    return {"id": lineup_id, "players": len(players)}


def publish_lineup(db, job, token, started, document):
    with db.snapshot_session(write=True) as session:
        if stopped := check_execution_in_session(session, token, time.monotonic() - started):
            raise WorkStopped(stopped)
        for old in session.scalars(select(GameLineup).where(GameLineup.batch_id == job["lineup_batch_id"])):
            if session.scalar(select(LineupLink).where(LineupLink.lineup_id == old.id)) is not None:
                raise ValueError("This game already has named players; remove names before regrouping")
            pids = {p["id"] for p in old.document["players"]}
            if any(
                pids.intersection(group.document["player_ids"])
                for group in session.scalars(select(LineupGrouping))
            ):
                raise ValueError("Undo this game's manual face groupings before automatic regrouping")
            delete_archive(db.settings.data_dir, old.document["templates"])
            session.delete(old)
        session.flush()
        session.add(
            GameLineup(
                id=document["id"],
                source_id=document["source_id"],
                batch_id=job["lineup_batch_id"],
                sha256=digest(document),
                document=document,
            )
        )
        row = session.get(Job, job["id"])
        row.state, row.lease_until = "succeeded", None
        row.document = {**row.document, "finished_at": now(), "progress": 1, "lineup_id": document["id"]}
        finish_execution(session, token, "succeeded", time.monotonic() - started)
