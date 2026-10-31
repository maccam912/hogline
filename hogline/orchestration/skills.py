"""Durable, source-bound vision jobs for short delivery sequences."""

import hashlib
import math
import time
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

from PIL import ImageDraw
from pydantic import Field, model_validator
from sqlalchemy import select

from hogline.adapters.sources import digest, video_link
from hogline.config import profile
from hogline.db import GameRow, Job, SkillSequence, now
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
from hogline.perception.call_targets import (
    PROMPT as CALL_PROMPT,
)
from hogline.perception.call_targets import (
    VERSION as CALL_VERSION,
)
from hogline.perception.call_targets import (
    CallEvidenceProposal,
    source_point,
    validate_targets,
)
from hogline.perception.shot_sequence import (
    SEQUENCE_PROMPT,
    VERSION,
    ActivitySequenceProposal,
    contact_sheets,
    validate_frame_references,
)
from hogline.perception.vision_calls import propose_call
from hogline.perception.vision_config import load_vision_config


class SequenceRequest(Model):
    source_id: str
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(gt=0)
    interval_seconds: float = Field(default=0.75, ge=0.25, le=2)
    view: Literal["composite", "perspective_left", "perspective_right", "house_top", "house_bottom"] = (
        "composite"
    )
    max_worker_seconds: int = Field(default=300, ge=180, le=600)
    purpose: Literal["activity", "pre_delivery_call"] = "activity"
    anchor_sequence_id: str | None = None
    anchor_delivery_index: int | None = Field(default=None, ge=0, le=7)
    anchor_sha256: str | None = None

    @model_validator(mode="after")
    def bounded(self):
        count = math.ceil((self.end_seconds - self.start_seconds) / self.interval_seconds)
        if not 2 <= count <= 16 or self.end_seconds - self.start_seconds > 30:
            raise ValueError("Choose 2–16 samples in a sequence of at most 30 seconds")
        anchors = (self.anchor_sequence_id, self.anchor_delivery_index, self.anchor_sha256)
        if self.purpose == "pre_delivery_call":
            if any(v is None for v in anchors) or self.view not in ("house_top", "house_bottom"):
                raise ValueError("Pre-delivery call evidence needs a house view and frozen delivery anchor")
        elif any(v is not None for v in anchors):
            raise ValueError("Delivery anchors are only used for pre-delivery call evidence")
        return self


def game_source(session, game_id, request):
    game = session.get(GameRow, game_id)
    if game is None or game.document.get("retired_into"):
        raise ValueError("Choose an active game for skill processing")
    if not any(
        s["source_id"] == request.source_id
        and s["start_seconds"] <= request.start_seconds
        and s["end_seconds"] >= request.end_seconds
        for s in game.document["spans"]
    ):
        raise ValueError("Sequence must lie inside a game source span")
    source = source_or_error(session, request.source_id)
    if getattr(request, "purpose", None) == "pre_delivery_call":
        anchor = session.get(SkillSequence, request.anchor_sequence_id)
        if (
            anchor is None
            or anchor.game_id != game_id
            or anchor.sha256 != request.anchor_sha256
            or digest(anchor.document) != anchor.sha256
        ):
            raise ValueError("Call evidence delivery anchor is missing or changed")
        doc = anchor.document
        if (
            doc["source_id"] != request.source_id
            or not compatible_game_scope(session, game, doc["game_revision"])
            or doc["source_metadata_sha256"] != source["metadata_sha256"]
        ):
            raise ValueError("Call evidence anchor no longer matches game/source")
        deliveries = doc["proposal"]["deliveries"]
        if request.anchor_delivery_index >= len(deliveries):
            raise ValueError("Call evidence anchor delivery is absent")
        start = doc["samples"][deliveries[request.anchor_delivery_index]["first_frame"]]["source_seconds"]
        if not start - 30 <= request.start_seconds < request.end_seconds <= start:
            raise ValueError("Call evidence must precede the observed delivery by at most 30 seconds")
    return game, source


def enqueue_sequence(db, game_id, request):
    config = load_vision_config()
    if not config.enabled or not config.api_key:
        raise ValueError("Configure and enable OpenAI vision in .env before queueing")
    with db.snapshot_session(write=True) as session:
        game, source = game_source(session, game_id, request)
        job = enqueue_in_session(session, sequence_payload(game, source, request, config.model, profile()))
        return {"id": job.id, "state": job.state, "request": job.document["request"]}


def sequence_payload(game, source, request, model, layout):
    return {
        "kind": "skill_sequence",
        "stage_version": CALL_VERSION if request.purpose == "pre_delivery_call" else VERSION,
        "decoder_version": DECODER_VERSION,
        "game_id": game.id,
        "game_revision": game.revision,
        "source_hash": source["metadata_sha256"],
        "vision_model": model,
        "profile": layout,
        "profile_sha256": digest(layout),
        "request": request.model_dump(),
    }


def pre_delivery_requests(candidate, anchor, lower_bound):
    """Both houses until camera-end mapping is independently established."""
    observation = min(candidate["observations"], key=lambda o: (o["start_seconds"], o["sequence_id"]))
    if anchor.id != observation["sequence_id"] or digest(anchor.document) != anchor.sha256:
        raise ValueError("Delivery anchor changed while planning call evidence")
    end = observation["start_seconds"]
    start = max(lower_bound, end - 16)
    if end - start < 2:
        return []
    return [
        SequenceRequest(
            source_id=candidate["source_id"],
            start_seconds=start,
            end_seconds=end,
            interval_seconds=1,
            view=view,
            purpose="pre_delivery_call",
            anchor_sequence_id=anchor.id,
            anchor_delivery_index=observation["delivery_index"],
            anchor_sha256=anchor.sha256,
        )
        for view in ("house_top", "house_bottom")
    ]


def enqueue_candidate_calls(db, game_id, candidate_id):
    from hogline.reconstruction.skill_candidates import delivery_candidates

    config = load_vision_config()
    if not config.enabled or not config.api_key:
        raise ValueError("Configure and enable vision before queueing call evidence")
    with db.snapshot_session(write=True) as session:
        candidates = delivery_candidates(sequences_in_session(session, game_id))
        candidate = next((c for c in candidates if c["id"] == candidate_id), None)
        if candidate is None:
            raise ValueError("Delivery candidate changed or is unavailable; reload")
        if candidate["ambiguous_overlap"]:
            raise ValueError("Resolve ambiguous delivery overlap before linking call evidence")
        observation = min(candidate["observations"], key=lambda o: (o["start_seconds"], o["sequence_id"]))
        anchor = session.get(SkillSequence, observation["sequence_id"])
        game = session.get(GameRow, game_id)
        span_start = min(
            s["start_seconds"]
            for s in game.document["spans"]
            if s["source_id"] == candidate["source_id"]
            and s["start_seconds"] <= observation["start_seconds"] <= s["end_seconds"]
        )
        requests = pre_delivery_requests(candidate, anchor, span_start)
        if not requests:
            raise ValueError("Not enough pre-delivery footage inside this game span")
        jobs = []
        for request in requests:
            game, source = game_source(session, game_id, request)
            job = enqueue_in_session(
                session, sequence_payload(game, source, request, config.model, profile())
            )
            jobs.append({"id": job.id, "state": job.state, "request": request.model_dump()})
        return {"jobs": jobs, "association": "precedes_candidate_unconfirmed_receiving_end"}


def execute_skill_sequence(db, job):
    from hogline.orchestration.worker import update_owned

    request = SequenceRequest.model_validate(job["request"])
    expected_version = CALL_VERSION if request.purpose == "pre_delivery_call" else VERSION
    if job["stage_version"] != expected_version or job["decoder_version"] != DECODER_VERSION:
        raise ValueError("Sequence implementation changed; enqueue a current request")
    with db.snapshot_session() as session:
        game, source = game_source(session, job["game_id"], request)
        if (
            not compatible_game_scope(session, game, job["game_revision"])
            or source["metadata_sha256"] != job["source_hash"]
        ):
            raise ValueError("Game or source changed; enqueue a current sequence")
        existing = session.scalar(select(SkillSequence).where(SkillSequence.job_id == job["id"]))
        if existing:
            raise ValueError("This job already published a sequence")
    config = load_vision_config()
    if config.model != job["vision_model"]:
        raise ValueError("Vision model changed after queueing")
    started, last_tick = time.monotonic(), 0
    token = job["lease_token"]

    def tick():
        nonlocal last_tick
        elapsed = time.monotonic() - started
        if elapsed > request.max_worker_seconds:
            raise ValueError("Skill sequence worker deadline reached")
        if elapsed - last_tick >= 5:
            check_execution(db, token, elapsed)
            update_owned(db, job["id"], token, wall_seconds=elapsed)
            last_tick = elapsed

    media = resolve_media(source)
    samples, frames = [], []
    for image, timing in decode_window(
        media["uri"],
        request.start_seconds,
        request.end_seconds,
        interval=request.interval_seconds,
        max_frames=16,
        timeout=120,
        tick=tick,
        source_time_origin=-source_seconds({"pts": 0, "time_base_num": 1, "time_base_den": 1}, source),
    ):
        tick()
        seconds = source_seconds(timing, source)
        if not request.start_seconds - 0.001 <= seconds < request.end_seconds + 0.001:
            raise ValueError("Decoded frame lies outside sequence")
        if request.purpose == "pre_delivery_call" and seconds >= request.end_seconds:
            raise ValueError("Pre-delivery evidence must be strictly before its anchor")
        samples.append(
            {
                "index": len(samples),
                "source_seconds": seconds,
                **timing,
                "decoded_rgb_sha256": hashlib.sha256(image.tobytes()).hexdigest(),
                "youtube_url": video_link(source, seconds),
                "width": image.width,
                "height": image.height,
            }
        )
        bounds = job["profile"]["views"][request.view] if request.view != "composite" else [0, 0, 1, 1]
        crop_bounds = tuple(
            round(v * (image.width if n % 2 == 0 else image.height)) for n, v in enumerate(bounds)
        )
        crop = image.crop(crop_bounds)
        samples[-1].update(
            {"view": request.view, "normalized_crop_bounds": bounds, "crop_bounds_px": crop_bounds}
        )
        frames.append((seconds, crop))
    sheets = contact_sheets(frames, request.view)
    budget = EvidenceBudget(db.settings.data_dir, 16, db.settings.max_cache_mb)
    budget.reserve_growth = lambda size: check_execution(db, token, time.monotonic() - started, size)
    assets = [save_image(image, db.settings.data_dir, budget) for _, image in sheets]
    tick()
    update_owned(db, job["id"], token, progress=0.6, sample_count=len(samples))
    context = {
        "game_id": job["game_id"],
        "game_revision": job["game_revision"],
        "source_id": source["id"],
        "source_metadata_sha256": source["metadata_sha256"],
        "version": expected_version,
        "profile_sha256": job["profile_sha256"],
        "samples": samples,
        "call_anchor": {
            "sequence_id": request.anchor_sequence_id,
            "delivery_index": request.anchor_delivery_index,
            "sha256": request.anchor_sha256,
        }
        if request.purpose == "pre_delivery_call"
        else None,
    }
    schema = CallEvidenceProposal if request.purpose == "pre_delivery_call" else ActivitySequenceProposal
    result = propose_call(
        db.settings.data_dir,
        sheets,
        config=config,
        prompt=CALL_PROMPT if request.purpose == "pre_delivery_call" else SEQUENCE_PROMPT,
        schema=schema,
        context=context,
    )
    proposal = schema.model_validate(result["proposal"])
    validate_frame_references(proposal, len(samples))
    target_assets = []
    if request.purpose == "pre_delivery_call":
        validate_targets(proposal, len(samples))
        for index, target in enumerate(proposal.targets):
            sample, frame = samples[target.frame], frames[target.frame][1]
            if sample["source_seconds"] >= request.end_seconds:
                raise ValueError("Target frame is not strictly before the delivery anchor")
            overlay = frame.copy()
            draw = ImageDraw.Draw(overlay)
            x, y = target.point[0] * frame.width, target.point[1] * frame.height
            draw.ellipse((x - 8, y - 8, x + 8, y + 8), outline="magenta", width=3)
            draw.line((x - 14, y, x + 14, y), fill="magenta", width=2)
            draw.line((x, y - 14, x, y + 14), fill="magenta", width=2)
            target_assets.append(
                {
                    "target_index": index,
                    "frame_index": target.frame,
                    "source_seconds": sample["source_seconds"],
                    **source_point(
                        target.point, sample["crop_bounds_px"], (sample["width"], sample["height"])
                    ),
                    "native_asset": save_image(frame, db.settings.data_dir, budget),
                    "overlay_asset": save_image(overlay, db.settings.data_dir, budget),
                    "coordinate_status": "uncalibrated_image_point",
                }
            )
    activity_assets = []
    for index, activity in enumerate(proposal.activities):
        frame = frames[activity.key_frame][1]
        box = [round(v * (frame.width if n % 2 == 0 else frame.height)) for n, v in enumerate(activity.bbox)]
        entry = {"activity_index": index, "frame_index": activity.key_frame, "bbox_in_view_px": box}
        if box[2] <= box[0] or box[3] <= box[1]:
            entry["unavailable_reason"] = "actor_box_smaller_than_one_pixel"
        else:
            entry["asset"] = save_image(frame.crop(tuple(box)), db.settings.data_dir, budget)
        activity_assets.append(entry)
    document = {
        **context,
        "id": str(uuid5(NAMESPACE_URL, f"skill-sequence:{job['id']}")),
        "job_id": job["id"],
        "created_at": now(),
        "request": request.model_dump(),
        "sheet_assets": assets,
        "activity_assets": activity_assets,
        "target_assets": target_assets,
        "vision_result_id": result["id"],
        "vision_result_sha256": digest(result),
        "proposal": proposal.model_dump(),
        "acceptance": "proposed",
    }
    with db.snapshot_session(write=True) as session:
        if stopped := check_execution_in_session(session, token, time.monotonic() - started):
            raise WorkStopped(stopped)
        game, current = game_source(session, job["game_id"], request)
        if (
            not compatible_game_scope(session, game, job["game_revision"])
            or current["metadata_sha256"] != job["source_hash"]
        ):
            raise ValueError("Game or source changed before publication")
        session.add(
            SkillSequence(
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
            "sequence_id": document["id"],
            "wall_seconds": time.monotonic() - started,
        }
        finish_execution(session, token, "succeeded", time.monotonic() - started)
    return document


def sequences_in_session(session, game_id):
    results = []
    game = session.get(GameRow, game_id)
    for row in session.scalars(select(SkillSequence).where(SkillSequence.game_id == game_id)):
        if digest(row.document) != row.sha256:
            raise ValueError("Skill sequence checksum mismatch")
        doc = row.document
        source = source_or_error(session, doc["source_id"])
        stale = (
            game is None
            or bool(game.document.get("retired_into"))
            or not compatible_game_scope(session, game, doc["game_revision"])
            or source["metadata_sha256"] != doc["source_metadata_sha256"]
        )
        if doc["request"].get("purpose") == "pre_delivery_call":
            anchor = session.get(SkillSequence, doc["request"]["anchor_sequence_id"])
            stale |= (
                anchor is None
                or anchor.sha256 != doc["request"]["anchor_sha256"]
                or digest(anchor.document) != anchor.sha256
            )
        results.append({**doc, "stale": stale})
    return results
