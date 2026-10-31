"""Bounded, local-CV motion evidence. Outputs never become accepted player scores."""

import hashlib
import math
import time
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

import numpy as np
from PIL import ImageDraw
from pydantic import Field, model_validator
from sqlalchemy import select

from hogline.adapters.sources import digest, video_link
from hogline.config import profile
from hogline.db import GameRow, Job, SkillMotion, SkillSequence, now
from hogline.domain.catalog import source_or_error
from hogline.domain.models import Model
from hogline.domain.skill_scope import compatible_game_scope
from hogline.media.frames import EvidenceBudget, resolve_media, save_image, source_seconds
from hogline.media.windows import VERSION as DECODER_VERSION
from hogline.media.windows import decode_window
from hogline.orchestration.plans import (
    WorkStopped,
    check_execution,
    check_execution_in_session,
    enqueue_in_session,
    finish_execution,
)
from hogline.orchestration.skills import game_source, sequences_in_session
from hogline.perception.people import MODEL_SHA256, optional_person_detector
from hogline.perception.perspective_stones import VERSION as PERSPECTIVE_VERSION
from hogline.perception.projective_house import VERSION as PROJECTION_VERSION
from hogline.perception.projective_house import diagnose_house_projection
from hogline.perception.shot_motion import (
    VERSION,
    delivery_path_proposals,
    measure_track,
    motion_detections,
    motion_tracks,
    perspective_detections,
    release_phase,
)
from hogline.reconstruction.skill_candidates import delivery_candidates

JOB_VERSION = "bounded-camera-motion-job-v6"


def motion_overlay(reference, tracks, priority_ids=None):
    overlay = reference.copy()
    draw = ImageDraw.Draw(overlay)
    for n, track in enumerate(tracks, 1):
        points = [tuple(p["center_px"]) for p in track["points"]]
        color = (
            "#7e8b86"
            if priority_ids is not None and track["id"] not in priority_ids
            else "#ff00ff"
            if track["color"] == "red"
            else "#008000"
        )
        draw.line(points, fill=color, width=3)
        for x, y in points:
            draw.ellipse((x - 3, y - 3, x + 3, y + 3), outline=color, width=2)
        draw.text(points[0], f"{n}", fill="black", stroke_width=1, stroke_fill="white")
    return overlay


class MotionRequest(Model):
    source_id: str
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(gt=0)
    reference_seconds: float = Field(ge=0)
    interval_seconds: float = Field(default=0.1, ge=0.05, le=0.2)
    view: Literal["house_top", "house_bottom", "perspective_left", "perspective_right"] = "house_top"
    max_worker_seconds: int = Field(default=300, ge=180, le=600)
    anchor_sequence_id: str | None = None
    anchor_delivery_index: int | None = Field(default=None, ge=0, le=7)
    anchor_sha256: str | None = None

    @model_validator(mode="after")
    def bounded(self):
        count = math.ceil((self.end_seconds - self.start_seconds) / self.interval_seconds)
        if not 3 <= count <= 160 or self.end_seconds - self.start_seconds > 16:
            raise ValueError("Choose 3–160 samples across at most 16 seconds")
        if not 0 <= self.start_seconds - self.reference_seconds <= 120:
            raise ValueError("Choose a reference at or within 120 seconds before motion")
        anchors = (self.anchor_sequence_id, self.anchor_delivery_index, self.anchor_sha256)
        if any(v is not None for v in anchors) and any(v is None for v in anchors):
            raise ValueError("Motion delivery anchor needs sequence, index and checksum")
        return self


def motion_source(session, game_id, request):
    game, source = game_source(session, game_id, request)
    if not any(
        span["source_id"] == request.source_id
        and span["start_seconds"] <= request.reference_seconds < span["end_seconds"]
        for span in game.document["spans"]
    ):
        raise ValueError("Reference frame must lie inside a game source span")
    if request.anchor_sequence_id:
        anchor = session.get(SkillSequence, request.anchor_sequence_id)
        if (
            anchor is None
            or anchor.game_id != game_id
            or anchor.sha256 != request.anchor_sha256
            or digest(anchor.document) != request.anchor_sha256
        ):
            raise ValueError("Motion delivery anchor is missing or changed")
        doc = anchor.document
        if (
            doc["source_id"] != request.source_id
            or not compatible_game_scope(session, game, doc["game_revision"])
            or doc["source_metadata_sha256"] != source["metadata_sha256"]
        ):
            raise ValueError("Motion delivery anchor no longer matches game/source")
        deliveries = doc["proposal"]["deliveries"]
        if request.anchor_delivery_index >= len(deliveries):
            raise ValueError("Motion delivery anchor is absent")
        delivery = deliveries[request.anchor_delivery_index]
        start = doc["samples"][delivery["first_frame"]]["source_seconds"]
        end = doc["samples"][delivery["last_frame"]]["source_seconds"]
        if not start - 3 <= request.start_seconds < request.end_seconds <= end + 8:
            raise ValueError("Anchored motion must stay near its delivery observation")
    return game, source


def enqueue_candidate_motion(db, game_id, candidate_id):
    layout = profile()
    with db.snapshot_session() as session:
        sequences = sequences_in_session(session, game_id)
        candidate = next((c for c in delivery_candidates(sequences) if c["id"] == candidate_id), None)
        if candidate is None or candidate["ambiguous_overlap"]:
            raise ValueError("Choose an unambiguous current delivery candidate")
        request = candidate_motion_request(session, game_id, candidate, layout)
    return enqueue_motion(db, game_id, request)


def candidate_motion_request(session, game_id, candidate, layout):
    """Choose a bounded camera clip from current, unambiguous delivery evidence."""
    observations = sorted(
        candidate["observations"],
        key=lambda o: (
            o["release_seconds"] is None,
            o["thrower_actor_id"] is None,
            o["end_seconds"] - o["start_seconds"],
            o["sequence_id"],
        ),
    )
    for observation in observations:
        sequence = session.get(SkillSequence, observation["sequence_id"])
        doc = sequence.document
        if doc.get("profile_sha256") != digest(layout):
            continue
        view = doc["request"].get("view", "composite")
        if view == "composite":
            tag = (observation["thrower_actor_id"] or "").split(":")[-1]
            activities = [
                a
                for a in doc["proposal"].get("activities", [])
                if a["actor_tag"] == tag and a["action"] == "delivery"
            ]
            if not activities:
                continue
            x1, y1, x2, y2 = activities[0]["bbox"]
            matches = [
                name
                for name in ("perspective_left", "perspective_right")
                if layout["views"][name][0] <= x1 < x2 <= layout["views"][name][2]
                and layout["views"][name][1] <= y1 < y2 <= layout["views"][name][3]
            ]
            if len(matches) != 1:
                continue
            view = matches[0]
        if view not in ("perspective_left", "perspective_right"):
            continue
        game = session.get(GameRow, game_id)
        center = observation["release_seconds"]
        if center is None:
            center = observation["end_seconds"]
        span = next(
            s
            for s in game.document["spans"]
            if s["source_id"] == doc["source_id"] and s["start_seconds"] <= center <= s["end_seconds"]
        )
        start, end = max(span["start_seconds"], center - 1), min(span["end_seconds"], center + 5)
        request = MotionRequest(
            source_id=doc["source_id"],
            start_seconds=start,
            end_seconds=end,
            reference_seconds=start,
            view=view,
            anchor_sequence_id=sequence.id,
            anchor_delivery_index=observation["delivery_index"],
            anchor_sha256=sequence.sha256,
        )
        return request
    else:
        raise ValueError("No supported perspective camera for this delivery; choose a clip manually")


def enqueue_motion(db, game_id, request):
    layout = profile()
    with db.snapshot_session(write=True) as session:
        game, source = motion_source(session, game_id, request)
        job = enqueue_in_session(session, motion_payload(game, source, request, layout))
        return {"id": job.id, "state": job.state, "request": job.document["request"]}


def motion_payload(game, source, request, layout):
    return {
        "kind": "skill_motion",
        "stage_version": JOB_VERSION,
        "projection_version": PROJECTION_VERSION,
        "perspective_handles_version": PERSPECTIVE_VERSION,
        "person_model_sha256": MODEL_SHA256,
        "decoder_version": DECODER_VERSION,
        "game_id": game.id,
        "game_revision": game.revision,
        "source_hash": source["metadata_sha256"],
        "profile": layout,
        "profile_sha256": digest(layout),
        "request": request.model_dump(),
    }


def execute_skill_motion(db, job):
    from hogline.orchestration.worker import update_owned

    request = MotionRequest.model_validate(job["request"])
    if (job["stage_version"], job["projection_version"], job["decoder_version"]) != (
        JOB_VERSION,
        PROJECTION_VERSION,
        DECODER_VERSION,
    ) or digest(job["profile"]) != job["profile_sha256"]:
        raise ValueError("Motion implementation/profile changed; enqueue a current request")
    if (job.get("perspective_handles_version"), job.get("person_model_sha256")) != (
        PERSPECTIVE_VERSION,
        MODEL_SHA256,
    ):
        raise ValueError("Perspective motion detector changed; enqueue a current request")
    with db.snapshot_session() as session:
        game, source = motion_source(session, job["game_id"], request)
        anchor_info = None
        if request.anchor_sequence_id:
            doc = session.get(SkillSequence, request.anchor_sequence_id).document
            delivery = doc["proposal"]["deliveries"][request.anchor_delivery_index]
            frame = delivery["release_frame"]
            anchor_info = {
                "sequence_id": request.anchor_sequence_id,
                "delivery_index": request.anchor_delivery_index,
                "sha256": request.anchor_sha256,
                "stone_color": delivery["stone_color"],
                "release_seconds": doc["samples"][frame]["source_seconds"] if frame is not None else None,
                "acceptance": "proposed",
            }
        if (
            not compatible_game_scope(session, game, job["game_revision"])
            or source["metadata_sha256"] != job["source_hash"]
        ):
            raise ValueError("Game or source changed; enqueue a current motion request")
        if session.scalar(select(SkillMotion).where(SkillMotion.job_id == job["id"])):
            raise ValueError("This job already published motion evidence")
    token, started, last_tick = job["lease_token"], time.monotonic(), 0

    def tick():
        nonlocal last_tick
        elapsed = time.monotonic() - started
        if elapsed > request.max_worker_seconds:
            raise ValueError("Motion worker deadline reached")
        if elapsed - last_tick >= 5:
            check_execution(db, token, elapsed)
            update_owned(db, job["id"], token, wall_seconds=elapsed)
            last_tick = elapsed

    media = resolve_media(source)
    bounds = job["profile"]["views"][request.view]
    time_origin = -source_seconds({"pts": 0, "time_base_num": 1, "time_base_den": 1}, source)

    def decode(start, end, limit, interval=None):
        yield from decode_window(
            media["uri"],
            start,
            end,
            interval=request.interval_seconds if interval is None else interval,
            max_frames=limit,
            timeout=120,
            tick=tick,
            source_time_origin=time_origin,
        )

    def crop_and_sample(image, timing, index):
        seconds = source_seconds(timing, source)
        pixel_bounds = [
            round(v * (image.width if i % 2 == 0 else image.height)) for i, v in enumerate(bounds)
        ]
        crop = image.crop(tuple(pixel_bounds))
        return crop, {
            "sample_index": index,
            "source_seconds": seconds,
            **timing,
            "decoded_rgb_sha256": hashlib.sha256(image.tobytes()).hexdigest(),
            "crop_rgb_sha256": hashlib.sha256(crop.tobytes()).hexdigest(),
            "youtube_url": video_link(source, seconds),
            "view": request.view,
            "normalized_crop_bounds": bounds,
            "pixel_crop_bounds": pixel_bounds,
            "source_width": image.width,
            "source_height": image.height,
            "crop_width": crop.width,
            "crop_height": crop.height,
            "requested_interval_seconds": request.interval_seconds,
        }

    reference = list(decode(request.reference_seconds, request.reference_seconds + 0.2 - 1e-6, 1, 0.2))
    if len(reference) != 1:
        raise ValueError("Reference frame unavailable")
    ref_crop, ref_sample = crop_and_sample(*reference[0], -1)
    if max(ref_crop.size) > 1080:
        raise ValueError("Motion pilot requires native camera crops no larger than 1080 pixels per side")
    if (
        not request.reference_seconds - 0.001
        <= ref_sample["source_seconds"]
        < request.reference_seconds + 0.2
    ):
        raise ValueError("Reference frame outside requested bounds")
    perspective = request.view.startswith("perspective_")
    person_detector, person_info = (
        optional_person_detector(db.settings.data_dir) if perspective else (None, {"status": "not_requested"})
    )
    projection = (
        {"status": "unavailable", "reasons": ["perspective_view_needs_sheet_calibration"]}
        if perspective
        else diagnose_house_projection(np.asarray(ref_crop))
    )
    budget = EvidenceBudget(db.settings.data_dir, 16, db.settings.max_cache_mb)
    budget.reserve_growth = lambda size: check_execution(db, token, time.monotonic() - started, size)
    ref_asset = save_image(ref_crop, db.settings.data_dir, budget)
    rows, retained, crops = [], [], {}
    retained_rgb_bytes = 0
    expected_count = math.ceil((request.end_seconds - request.start_seconds) / request.interval_seconds)
    selected_indices = set(np.linspace(0, expected_count - 1, min(11, expected_count), dtype=int))
    last_crop = None
    for image, timing in decode(request.start_seconds, request.end_seconds, 160):
        tick()
        crop, row = crop_and_sample(image, timing, len(rows))
        if not request.start_seconds - 0.001 <= row["source_seconds"] < request.end_seconds + 0.001:
            raise ValueError("Motion frame outside requested bounds")
        if crop.size != ref_crop.size or row["pixel_crop_bounds"] != ref_sample["pixel_crop_bounds"]:
            raise ValueError("Video geometry changed since reference")
        people = person_detector.detect(np.asarray(crop)) if person_detector else []
        # Only box geometry is needed for exclusions, not appearance descriptors.
        row["person_boxes"] = [p["bbox_px"] for p in people]
        row.update(
            perspective_detections(np.asarray(crop), people)
            if perspective
            else motion_detections(np.asarray(crop), projection)
        )
        rows.append(row)
        if not perspective or row["sample_index"] in selected_indices:
            retained_rgb_bytes += crop.width * crop.height * 3
            if retained_rgb_bytes > 128 * 1024**2:
                raise ValueError("Motion crop memory limit reached; shorten the requested clip")
            crops[row["sample_index"]] = crop
        last_crop = crop
    if perspective and rows:
        crops[rows[-1]["sample_index"]] = last_crop
    track_result = motion_tracks(rows, digest({"job": job["id"], "samples": rows}))
    moving = [t for t in track_result["tracks"] if t["motion_class"] == "position_change_observed"]
    for track in moving:
        track["relative_release_phase"] = release_phase(
            track, anchor_info["release_seconds"] if anchor_info else None
        )
        track["matches_delivery_color"] = (
            track["color"] == anchor_info["stone_color"]
            if anchor_info and anchor_info["stone_color"] != "unknown"
            else None
        )
    measurements = [measure_track(track, projection) for track in moving]
    usable = [m for m in measurements if m["status"] == "experimental_measurement"]
    # Up to 12 exact native crops, selected evenly from observed moving fragments.
    indices = (
        sorted(crops) if perspective else sorted({p["sample_index"] for m in moving for p in m["points"]})
    )
    if len(indices) > 12:
        indices = [indices[i] for i in np.linspace(0, len(indices) - 1, 12, dtype=int)]
    for index in indices:
        retained.append(
            {"sample_index": index, "asset": save_image(crops[index], db.settings.data_dir, budget)}
        )
    paths = delivery_path_proposals(moving, anchor_info, job["profile"].get("id"))
    priority_ids = {p["track_id"] for p in paths if p["status"] == "review_first"}
    overlay_asset = save_image(
        motion_overlay(ref_crop, moving, priority_ids or None), db.settings.data_dir, budget
    )
    document = {
        "id": str(uuid5(NAMESPACE_URL, f"skill-motion:{job['id']}")),
        "job_id": job["id"],
        "game_id": job["game_id"],
        "game_revision": job["game_revision"],
        "source_id": source["id"],
        "source_metadata_sha256": source["metadata_sha256"],
        "profile_sha256": job["profile_sha256"],
        "version": VERSION,
        "decoder_version": DECODER_VERSION,
        "created_at": now(),
        "request": request.model_dump(),
        "delivery_anchor": anchor_info,
        "person_detector": person_info,
        "image_sampling": "eleven_even_samples_and_last" if perspective else "moving_fragment_samples",
        "reference_sample": ref_sample,
        "reference_asset": ref_asset,
        "overlay_asset": overlay_asset,
        "retained_samples": retained,
        "projection": projection,
        "samples": rows,
        "tracking": track_result,
        "measurements": measurements,
        "image_motion_tracks": moving,
        "delivery_path_proposals": paths,
        "acceptance": "proposed",
        "status": "experimental_measurements" if usable else "image_motion_only" if moving else "unavailable",
        "weight_multiplier": None,
        "aim_error_degrees": None,
        "limitations": [
            "static_camera_assumed_not_verified",
            "local_colored_patch_size_prior_not_object_verification",
            "no_release_or_called_target_established",
            "no_automatic_player_score",
            "timing_after_proposed_release_does_not_verify_free_travel_or_physical_stone_identity",
            *(["perspective_candidates_have_no_physical_scale"] if perspective else []),
        ],
    }
    with db.snapshot_session(write=True) as session:
        if stopped := check_execution_in_session(session, token, time.monotonic() - started):
            raise WorkStopped(stopped)
        game, current = motion_source(session, job["game_id"], request)
        if (
            not compatible_game_scope(session, game, job["game_revision"])
            or current["metadata_sha256"] != job["source_hash"]
        ):
            raise ValueError("Game or source changed before motion publication")
        session.add(
            SkillMotion(
                id=document["id"],
                game_id=game.id,
                job_id=job["id"],
                sha256=digest(document),
                document=document,
            )
        )
        row = session.get(Job, job["id"])
        row.state, row.lease_until = "succeeded", None
        row.document = {
            **row.document,
            "progress": 1,
            "finished_at": now(),
            "motion_id": document["id"],
            "wall_seconds": time.monotonic() - started,
        }
        finish_execution(session, token, "succeeded", time.monotonic() - started)
    return document


def motions_in_session(session, game_id):
    game = session.get(GameRow, game_id)
    results = []
    for row in session.scalars(select(SkillMotion).where(SkillMotion.game_id == game_id)):
        doc = row.document
        if digest(doc) != row.sha256:
            raise ValueError("Motion evidence checksum mismatch")
        source = source_or_error(session, doc["source_id"])
        stale = (
            game is None
            or bool(game.document.get("retired_into"))
            or not compatible_game_scope(session, game, doc["game_revision"])
            or source["metadata_sha256"] != doc["source_metadata_sha256"]
        )
        anchor = doc.get("delivery_anchor")
        if anchor:
            row = session.get(SkillSequence, anchor["sequence_id"])
            stale |= row is None or row.sha256 != anchor["sha256"] or digest(row.document) != anchor["sha256"]
        parent = doc.get("retained_detection_parent")
        if parent:
            row = session.get(SkillMotion, parent["motion_id"])
            stale |= row is None or row.sha256 != parent["sha256"] or digest(row.document) != parent["sha256"]
        results.append({**doc, "stale": stale})
    return results
