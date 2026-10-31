"""Persisted, bounded local motion follow-ups of existing delivery proposals."""

import math

from pydantic import Field
from sqlalchemy import select

from hogline.adapters.sources import digest
from hogline.config import profile
from hogline.db import GameRow, Job, SkillMotion, SkillMotionBatch, now
from hogline.domain.catalog import Conflict
from hogline.domain.models import Model
from hogline.domain.skill_scope import compatible_game_scope
from hogline.domain.skills import SkillDocument, read_skills
from hogline.orchestration.plans import enqueue_in_session
from hogline.orchestration.skill_campaigns import CampaignControl, assign, checked
from hogline.orchestration.skill_motion import (
    JOB_VERSION,
    MotionRequest,
    candidate_motion_request,
    motion_payload,
    motion_source,
    motions_in_session,
)
from hogline.orchestration.skills import sequences_in_session
from hogline.perception.shot_motion import VERSION as TRACK_VERSION
from hogline.reconstruction.skill_candidates import delivery_candidates

VERSION = "bounded-local-motion-batch-v1"


class MotionBatchRequest(Model):
    max_candidates: int = Field(default=4, ge=1, le=12)


class MotionBatchStart(Model):
    request: MotionBatchRequest
    plan_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


def plan_batch(session, game_id, request):
    review = read_skills(session, game_id)
    if review["stale"]:
        raise ValueError("Refresh the saved skill review before planning motion")
    game = session.get(GameRow, game_id)
    layout = profile()
    shots = SkillDocument.model_validate(review["document"]).shots
    candidates = delivery_candidates(sequences_in_session(session, game_id), shots)
    motions = [
        m
        for m in motions_in_session(session, game_id)
        if not m["stale"] and m["version"] == TRACK_VERSION and m.get("profile_sha256") == digest(layout)
    ]
    units, omitted = [], []
    for candidate in candidates:
        reason = None
        if candidate["excluded_shot_ids"]:
            reason = "excluded_by_review"
        elif candidate["ambiguous_overlap"] or candidate["duplicate_review"]:
            reason = "ambiguous_or_duplicate_delivery"
        anchors = {(o["sequence_id"], o["delivery_index"]) for o in candidate["observations"]}
        existing = [
            m
            for m in motions
            if (
                (m.get("delivery_anchor") or {}).get("sequence_id"),
                (m.get("delivery_anchor") or {}).get("delivery_index"),
            )
            in anchors
        ]
        if not reason and existing:
            reason = "current_motion_already_available"
        if not reason:
            try:
                clip = candidate_motion_request(session, game_id, candidate, layout)
                _, source = motion_source(session, game_id, clip)
            except ValueError as exc:
                reason = str(exc)
        if not reason and len(units) >= request.max_candidates:
            reason = "beyond_selected_limit"
        if reason:
            omitted.append(
                {
                    "candidate_id": candidate["id"],
                    "reason": reason,
                    "motion_ids": sorted(m["id"] for m in existing),
                }
            )
            continue
        units.append(
            {
                "candidate_id": candidate["id"],
                "stone_color": candidate["stone_color"],
                "request": clip.model_dump(),
                "source_hash": source["metadata_sha256"],
                "release_known": any(
                    o["sequence_id"] == clip.anchor_sequence_id
                    and o["delivery_index"] == clip.anchor_delivery_index
                    and o["release_seconds"] is not None
                    for o in candidate["observations"]
                ),
            }
        )
    return {
        "version": VERSION,
        "job_version": JOB_VERSION,
        "tracking_version": TRACK_VERSION,
        "game_id": game_id,
        "game_revision": game.revision,
        "review_revision": review["revision"],
        "profile": layout,
        "profile_sha256": digest(layout),
        "request": request.model_dump(),
        "candidate_count": len(candidates),
        "units": units,
        "omitted": omitted,
        "maximum_decoded_samples": sum(
            1 + math.ceil((u["request"]["end_seconds"] - u["request"]["start_seconds"]) / 0.1) for u in units
        ),
        "notice": "Local tracking only; no OpenAI requests. Motion paths remain unconfirmed proposals.",
    }


def preview_batch(db, game_id, request):
    with db.snapshot_session() as session:
        plan = plan_batch(session, game_id, request)
    return {"plan": plan, "plan_sha256": digest(plan)}


def start_batch(db, game_id, start):
    with db.snapshot_session(write=True) as session:
        existing = session.scalar(
            select(SkillMotionBatch).where(SkillMotionBatch.plan_key == start.plan_sha256)
        )
        if existing is not None:
            if (
                existing.game_id != game_id
                or checked(existing)["plan"]["request"] != start.request.model_dump()
            ):
                raise ValueError("Motion plan does not match this game/request")
            return batch_view(session, existing)
        plan = plan_batch(session, game_id, start.request)
        if digest(plan) != start.plan_sha256:
            raise Conflict("Delivery evidence changed; preview the current motion plan")
        if not plan["units"]:
            raise ValueError("No new supported delivery clips in this plan")
        document = {
            "plan": plan,
            "units": [{**u, "job_id": None} for u in plan["units"]],
            "created_at": now(),
            "last_dispatched_at": None,
            "pause_reason": None,
        }
        row = SkillMotionBatch(
            game_id=game_id, plan_key=digest(plan), document=document, sha256=digest(document)
        )
        session.add(row)
        session.flush()
        return batch_view(session, row)


def context_error(session, plan):
    game = session.get(GameRow, plan["game_id"])
    if (
        game is None
        or game.document.get("retired_into")
        or not compatible_game_scope(session, game, plan["game_revision"])
    ):
        return "Game changed; preview a new motion plan"
    if (plan["version"], plan["job_version"], plan["tracking_version"], plan["profile_sha256"]) != (
        VERSION,
        JOB_VERSION,
        TRACK_VERSION,
        digest(profile()),
    ):
        return "Motion methods or camera profile changed; preview a new motion plan"
    review = read_skills(session, plan["game_id"])
    if review["stale"] or review["revision"] != plan["review_revision"]:
        return "Skill review changed; preview a new motion plan to preserve exclusions"
    try:
        for unit in plan["units"]:
            _, source = motion_source(session, plan["game_id"], MotionRequest.model_validate(unit["request"]))
            if source["metadata_sha256"] != unit["source_hash"]:
                return "Source changed; preview a new motion plan"
    except ValueError as exc:
        return str(exc)
    return None


def advance_motion_batches(db):
    """Admit at most one local motion clip globally; never use the vision configuration."""
    with db.snapshot_session(write=True) as session:
        rows = list(session.scalars(select(SkillMotionBatch).where(SkillMotionBatch.state == "active")))
        if not rows:
            return
        busy = (
            session.scalar(
                select(Job.id)
                .where(
                    Job.state.in_(["queued", "running"]), Job.document["kind"].as_string() == "skill_motion"
                )
                .limit(1)
            )
            is not None
        )
        for row in sorted(rows, key=lambda r: (checked(r).get("last_dispatched_at") or "", r.id)):
            doc = checked(row)
            if error := context_error(session, doc["plan"]):
                assign(row, {**doc, "pause_reason": error}, "paused")
                continue
            pending = None
            for i, unit in enumerate(doc["units"]):
                job = session.get(Job, unit["job_id"]) if unit["job_id"] else None
                if job is not None and job.state == "succeeded":
                    motion = session.scalar(select(SkillMotion).where(SkillMotion.job_id == job.id))
                    if motion is not None and digest(motion.document) == motion.sha256:
                        continue
                    assign(
                        row,
                        {**doc, "pause_reason": "Completed motion evidence is missing or changed"},
                        "paused",
                    )
                    pending = False
                    break
                pending = (i, unit, job)
                break
            if pending is False:
                continue
            if pending is None:
                assign(row, {**doc, "completed_at": now()}, "completed")
                continue
            i, unit, job = pending
            if job is not None:
                if job.state in ("failed", "cancelled"):
                    assign(
                        row,
                        {**doc, "pause_reason": f"Motion clip {job.state}; inspect its job before retrying"},
                        "paused",
                    )
                continue
            if busy:
                continue
            clip = MotionRequest.model_validate(unit["request"])
            game, source = motion_source(session, row.game_id, clip)
            payload = motion_payload(game, source, clip, doc["plan"]["profile"])
            # Existing failures are surfaced, never silently retried by a batch.
            job = session.scalar(select(Job).where(Job.job_key == digest(payload)))
            if job is None:
                job = enqueue_in_session(session, payload)
            units = [dict(u) for u in doc["units"]]
            units[i]["job_id"] = job.id
            assign(row, {**doc, "units": units, "last_dispatched_at": now()})
            busy = job.state in ("queued", "running")


def batch_view(session, row):
    document = checked(row)
    units = []
    for unit in document["units"]:
        job = session.get(Job, unit["job_id"]) if unit["job_id"] else None
        units.append(
            {
                **unit,
                "state": job.state if job else "not_queued",
                "error": job.document.get("error") if job else None,
                "motion_id": job.document.get("motion_id") if job else None,
            }
        )
    return {
        **document,
        "units": units,
        "id": row.id,
        "game_id": row.game_id,
        "revision": row.revision,
        "state": row.state,
    }


def batches_in_session(session, game_id):
    return [
        batch_view(session, row)
        for row in session.scalars(select(SkillMotionBatch).where(SkillMotionBatch.game_id == game_id))
    ]


def control_batch(db, batch_id, control: CampaignControl):
    with db.snapshot_session(write=True) as session:
        row = session.get(SkillMotionBatch, batch_id)
        if row is None:
            raise ValueError("Motion batch not found")
        doc = checked(row)
        if row.revision != control.expected_revision:
            raise Conflict("Motion processing changed; reload its current state")
        if row.state in ("completed", "cancelled"):
            raise ValueError("Motion batch is already terminal")
        if control.action == "resume":
            if error := context_error(session, doc["plan"]):
                raise ValueError(error)
            assign(row, {**doc, "pause_reason": None}, "active")
        else:
            assign(
                row,
                {**doc, "pause_reason": "Stopped by operator; admitted clip may finish"},
                "paused" if control.action == "pause" else "cancelled",
            )
        session.flush()
        return batch_view(session, row)
