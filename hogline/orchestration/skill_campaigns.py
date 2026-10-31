"""Resumable excerpt surveys and delivery refinements, admitting one vision job at a time."""

import math
from typing import Literal

from pydantic import Field, model_validator
from sqlalchemy import select

from hogline.adapters.sources import digest
from hogline.config import profile
from hogline.db import GameRow, Job, SkillCampaign, SkillSequence, now
from hogline.domain.catalog import Conflict, source_or_error
from hogline.domain.models import Model
from hogline.domain.skill_scope import compatible_game_scope
from hogline.media.windows import VERSION as DECODER_VERSION
from hogline.orchestration.plans import enqueue_in_session
from hogline.orchestration.skills import SequenceRequest, game_source, pre_delivery_requests, sequence_payload
from hogline.perception.call_targets import VERSION as CALL_VERSION
from hogline.perception.shot_sequence import VERSION as SEQUENCE_VERSION
from hogline.perception.vision_budget import RESERVATION_CENTS, budget_status
from hogline.perception.vision_config import load_vision_config
from hogline.reconstruction.skill_candidates import VERSION as CANDIDATE_VERSION
from hogline.reconstruction.skill_candidates import delivery_candidates

VERSION = "bounded-skill-excerpt-campaign-v2"


class CampaignRequest(Model):
    source_id: str
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(gt=0)
    window_seconds: int = Field(default=30, ge=8, le=30)
    overlap_seconds: int = Field(default=4, ge=2, le=6)
    interval_seconds: float = Field(default=2, ge=0.5, le=2)
    view: Literal["composite", "perspective_left", "perspective_right"] = "composite"
    max_refinements: int = Field(default=4, ge=0, le=8)
    max_call_candidates: int = Field(default=1, ge=0, le=4)

    @model_validator(mode="after")
    def bounded(self):
        if not 4 <= self.end_seconds - self.start_seconds <= 1800:
            raise ValueError("Choose an excerpt from 4 seconds to 30 minutes")
        if (
            self.overlap_seconds >= self.window_seconds
            or math.ceil(self.window_seconds / self.interval_seconds) > 16
        ):
            raise ValueError("Survey windows need at most 16 samples and a smaller overlap")
        return self


class CampaignControl(Model):
    action: Literal["pause", "resume", "cancel"]
    expected_revision: int = Field(ge=1)


def survey_requests(request):
    units, start = [], request.start_seconds
    while start < request.end_seconds:
        end = min(start + request.window_seconds, request.end_seconds)
        units.append(
            SequenceRequest(
                source_id=request.source_id,
                start_seconds=start,
                end_seconds=end,
                interval_seconds=request.interval_seconds,
                view=request.view,
            ).model_dump()
        )
        if end == request.end_seconds:
            break
        start = end - request.overlap_seconds
    if len(units) > 120:
        raise ValueError("This excerpt requires more than 120 survey clips; use a shorter excerpt")
    return units


def checked(row):
    if digest(row.document) != row.sha256:
        raise ValueError("Skill campaign checksum mismatch")
    return row.document


def assign(row, document, state=None):
    row.document, row.sha256 = document, digest(document)
    if state is not None:
        row.state = state


def campaign_plan(session, game_id, request):
    game, source = game_source(session, game_id, request)
    config = load_vision_config()
    layout = profile()
    return {
        "version": VERSION,
        "sequence_version": SEQUENCE_VERSION,
        "decoder_version": DECODER_VERSION,
        "candidate_version": CANDIDATE_VERSION,
        "call_version": CALL_VERSION,
        "game_id": game_id,
        "game_revision": game.revision,
        "source_hash": source["metadata_sha256"],
        "vision_model": config.model,
        "profile": layout,
        "profile_sha256": digest(layout),
        "request": request.model_dump(),
        "survey_requests": survey_requests(request),
        "coverage_kind": "overlapping_sparse_sequences_with_bounded_delivery_refinements",
        "notice": "Completing a survey does not confirm all deliveries, game boundaries, roles or scores.",
    }


def preview_campaign(db, game_id, request):
    with db.snapshot_session() as session:
        plan = campaign_plan(session, game_id, request)
    return {
        "plan_sha256": digest(plan),
        "survey_clips": len(plan["survey_requests"]),
        "maximum_refinement_clips": request.max_refinements,
        "maximum_call_clips": request.max_call_candidates * 2,
        "plan": plan,
    }


def start_campaign(db, game_id, request):
    config = load_vision_config()
    if not config.enabled or not config.api_key:
        raise ValueError("Configure and enable vision before starting excerpt processing")
    with db.snapshot_session(write=True) as session:
        plan = campaign_plan(session, game_id, request)
        key = digest(plan)
        row = session.scalar(select(SkillCampaign).where(SkillCampaign.plan_key == key))
        if row is None:
            document = {
                "plan": plan,
                "created_at": now(),
                "phase": "survey",
                "pause_reason": None,
                "survey": [{"request": r, "job_id": None} for r in plan["survey_requests"]],
                "refinements": [],
                "refinements_planned": False,
                "last_dispatched_at": None,
            }
            row = SkillCampaign(game_id=game_id, plan_key=key, document=document, sha256=digest(document))
            session.add(row)
            session.flush()
        return campaign_view(session, row)


def context_error(session, plan, config):
    game = session.get(GameRow, plan["game_id"])
    if (
        game is None
        or game.document.get("retired_into")
        or not compatible_game_scope(session, game, plan["game_revision"])
    ):
        return "Game changed; start a new excerpt plan"
    source = source_or_error(session, plan["request"]["source_id"])
    if source["metadata_sha256"] != plan["source_hash"]:
        return "Source changed; start a new excerpt plan"
    if (
        plan["version"] != VERSION
        or plan["sequence_version"] != SEQUENCE_VERSION
        or plan["decoder_version"] != DECODER_VERSION
        or plan.get("candidate_version") != CANDIDATE_VERSION
        or plan.get("call_version") != CALL_VERSION
        or digest(profile()) != plan["profile_sha256"]
    ):
        return "Processing methods or camera profile changed; start a new excerpt plan"
    if not config.enabled or not config.api_key or config.model != plan["vision_model"]:
        return "Vision configuration changed; restore it before resuming"
    return None


def plan_refinements(session, document):
    plan = document["plan"]
    request = CampaignRequest.model_validate(plan["request"])
    job_ids = [unit["job_id"] for unit in document["survey"]]
    sequences = []
    for row in session.scalars(select(SkillSequence).where(SkillSequence.job_id.in_(job_ids))):
        if digest(row.document) != row.sha256:
            raise ValueError("Survey sequence checksum mismatch")
        sequences.append(row.document)
    if len(sequences) != len(set(job_ids)):
        raise ValueError("A completed survey job has no sequence evidence")
    candidates = delivery_candidates(sequences)
    index = {s["id"]: s for s in sequences}
    refinements = []
    for candidate in candidates[: request.max_refinements]:
        observation = next(
            (o for o in candidate["observations"] if o["thrower_actor_id"]), candidate["observations"][0]
        )
        sequence = index[observation["sequence_id"]]
        view = sequence["request"]["view"]
        tag = sequence["proposal"]["deliveries"][observation["delivery_index"]].get("thrower_actor_tag")
        if view == "composite" and tag:
            activities = [
                a
                for a in sequence["proposal"].get("activities", [])
                if a["actor_tag"] == tag and a["action"] == "delivery"
            ]
            if activities:
                x1, y1, x2, y2 = activities[0]["bbox"]
                for name in ("perspective_left", "perspective_right"):
                    bx1, by1, bx2, by2 = plan["profile"]["views"][name]
                    if bx1 <= (x1 + x2) / 2 <= bx2 and by1 <= (y1 + y2) / 2 <= by2:
                        view = name
                        break
        duration = min(8, request.end_seconds - request.start_seconds)
        start = max(
            request.start_seconds, min(candidate["start_seconds"] - 2, request.end_seconds - duration)
        )
        clip = SequenceRequest(
            source_id=request.source_id,
            start_seconds=start,
            end_seconds=start + duration,
            interval_seconds=0.5,
            view=view,
        )
        refinements.append({"request": clip.model_dump(), "job_id": None, "candidate_id": candidate["id"]})
    for candidate in [c for c in candidates if not c["ambiguous_overlap"]][: request.max_call_candidates]:
        observation = min(candidate["observations"], key=lambda o: (o["start_seconds"], o["sequence_id"]))
        anchor = session.get(SkillSequence, observation["sequence_id"])
        for clip in pre_delivery_requests(candidate, anchor, request.start_seconds):
            refinements.append(
                {
                    "request": clip.model_dump(),
                    "job_id": None,
                    "candidate_id": candidate["id"],
                    "purpose": "pre_delivery_call",
                }
            )
    return refinements, len(candidates)


def advance_skill_campaigns(db):
    """Short writer transaction. One outstanding vision job globally before admitting another."""
    with db.snapshot_session(write=True) as session:
        rows = list(session.scalars(select(SkillCampaign).where(SkillCampaign.state == "active")))
        if not rows:
            return
        try:
            config = load_vision_config()
        except ValueError:
            for row in rows:
                assign(
                    row,
                    {**checked(row), "pause_reason": "Vision configuration is invalid; check local settings"},
                    "paused",
                )
            return
        busy = (
            session.scalar(
                select(Job.id)
                .where(
                    Job.state.in_(["queued", "running"]), Job.document["kind"].as_string() == "skill_sequence"
                )
                .limit(1)
            )
            is not None
        )
        # Rotate between excerpts instead of letting one long recording monopolize requests.
        rows.sort(key=lambda row: (checked(row).get("last_dispatched_at") or "", row.id))
        for row in rows:
            document = {**checked(row)}
            plan = document["plan"]
            try:
                error = context_error(session, plan, config)
            except ValueError as exc:
                error = str(exc)
            if error:
                assign(row, {**document, "pause_reason": error}, "paused")
                continue
            units = document["survey"] if document["phase"] == "survey" else document["refinements"]
            pending = None
            for index, unit in enumerate(units):
                job = session.get(Job, unit["job_id"]) if unit["job_id"] else None
                if job is not None and job.state == "succeeded":
                    continue
                pending = (index, unit, job)
                break
            if pending is None:
                if document["phase"] == "survey":
                    try:
                        refinements, count = plan_refinements(session, document)
                    except ValueError as exc:
                        assign(row, {**document, "pause_reason": str(exc)}, "paused")
                        continue
                    document = {
                        **document,
                        "phase": "refinement",
                        "refinements": refinements,
                        "refinements_planned": True,
                        "delivery_candidate_count": count,
                        "deferred_refinements": max(0, count - plan["request"]["max_refinements"]),
                    }
                    assign(row, document)
                    if refinements:
                        continue
                assign(row, {**document, "completed_at": now()}, "completed")
                continue
            index, unit, job = pending
            if job is not None:
                if job.state in ("failed", "cancelled"):
                    assign(
                        row,
                        {
                            **document,
                            "pause_reason": f"Clip {job.state}: {job.document.get('error') or job.id}",
                        },
                        "paused",
                    )
                continue
            if busy:
                continue
            clip = SequenceRequest.model_validate(unit["request"])
            game, source = game_source(session, row.game_id, clip)
            payload = sequence_payload(game, source, clip, plan["vision_model"], plan["profile"])
            job = session.scalar(select(Job).where(Job.job_key == digest(payload)))
            if job is None:
                status = budget_status(db.settings.data_dir / "skill-vision")
                if status["committed_cents"] + RESERVATION_CENTS > int(config.budget_usd * 100):
                    assign(
                        row, {**document, "pause_reason": "Local vision reservation budget reached"}, "paused"
                    )
                    continue
                job = enqueue_in_session(session, payload)
            updated = [dict(u) for u in units]
            updated[index]["job_id"] = job.id
            field = "survey" if document["phase"] == "survey" else "refinements"
            assign(row, {**document, field: updated, "last_dispatched_at": now()})
            busy = job.state in ("queued", "running")


def campaign_view(session, row):
    document = checked(row)
    game = session.get(GameRow, row.game_id)
    source = source_or_error(session, document["plan"]["request"]["source_id"])
    stale = (
        game is None
        or bool(game.document.get("retired_into"))
        or not compatible_game_scope(session, game, document["plan"]["game_revision"])
        or source["metadata_sha256"] != document["plan"]["source_hash"]
    )
    identifiers = [u["job_id"] for field in ("survey", "refinements") for u in document[field] if u["job_id"]]
    jobs = (
        {j.id: j for j in session.scalars(select(Job).where(Job.id.in_(identifiers)))} if identifiers else {}
    )
    view = {
        **document,
        "id": row.id,
        "game_id": row.game_id,
        "revision": row.revision,
        "state": row.state,
        "stale": stale,
    }
    for field in ("survey", "refinements"):
        view[field] = [
            {**unit, "state": jobs[unit["job_id"]].state if unit["job_id"] in jobs else "not_queued"}
            for unit in document[field]
        ]
    return view


def campaigns_in_session(session, game_id):
    return [
        campaign_view(session, row)
        for row in session.scalars(select(SkillCampaign).where(SkillCampaign.game_id == game_id))
    ]


def control_campaign(db, campaign_id, control):
    with db.snapshot_session(write=True) as session:
        row = session.get(SkillCampaign, campaign_id)
        if row is None:
            raise ValueError("Skill campaign not found")
        document = checked(row)
        if row.revision != control.expected_revision:
            raise Conflict("Excerpt processing changed; reload its current state")
        if row.state in ("completed", "cancelled"):
            raise ValueError("This excerpt processing run is already terminal")
        if control.action == "resume":
            if error := context_error(session, document["plan"], load_vision_config()):
                raise ValueError(error)
            assign(row, {**document, "pause_reason": None}, "active")
        else:
            state = "paused" if control.action == "pause" else "cancelled"
            assign(row, {**document, "pause_reason": "Stopped by operator; admitted clip may finish"}, state)
        session.flush()
        return campaign_view(session, row)
