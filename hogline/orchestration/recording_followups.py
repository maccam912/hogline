"""Durable, idempotent follow-up work for recording analyses; proposals stay unconfirmed."""

import logging
import time
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select

from hogline.db import GameRow, ProcessingBatch, SkillCampaign, SkillMotionBatch, Source
from hogline.domain.catalog import save_in_session
from hogline.domain.models import Game, GameEdit, Span
from hogline.media.post_live import has_verified_download
from hogline.orchestration.skill_campaigns import CampaignRequest, start_campaign
from hogline.orchestration.skill_motion_batches import (
    MotionBatchRequest,
    MotionBatchStart,
    preview_batch,
    start_batch,
)
from hogline.perception.vision_config import load_vision_config

_next_check = 0
log = logging.getLogger(__name__)


def prepare_games(db, source_id):
    with db.snapshot_session(write=True) as session:
        source = session.get(Source, source_id).document
        games = [
            g
            for g in session.scalars(select(GameRow))
            if not g.document.get("retired_into")
            and any(s["source_id"] == source_id for s in g.document["spans"])
        ]
        if games:
            return [(g.id, g.document["spans"]) for g in games]
        if not source.get("sheet") or len(source.get("title_teams", [])) != 2:
            raise ValueError("Review game boundaries before automatic skills: no single matchup in the title")
        latest = session.scalar(
            select(ProcessingBatch)
            .where(
                ProcessingBatch.document["campaign_purpose"].as_string() == "recording_analysis",
                ProcessingBatch.document["request"]["source_ids"][0].as_string() == source_id,
            )
            .order_by(ProcessingBatch.document["created_at"].as_string().desc())
        )
        if latest and latest.document.get("result_summary", {}).get("candidate_games", 0) > 1:
            raise ValueError("Review the multiple game boundaries before starting skills")
        identifier = str(uuid5(NAMESPACE_URL, f"hogline:recording-draft:{source_id}"))
        game = Game(
            id=identifier,
            sheet=source["sheet"],
            label=" vs ".join(source["title_teams"])[:200],
            spans=[Span(source_id=source_id, start_seconds=0, end_seconds=source["duration_seconds"])],
            note=(
                "Automatic draft from the recording title. Entire recording is provisional; "
                "review boundaries, teams, and ends. Stone colors are unassigned."
            ),
        )
        save_in_session(
            session,
            GameEdit(
                game=game,
                expected_revision=0,
                author="recording-analysis",
                reason="Create an unconfirmed workspace for automatic delivery and skill evidence",
            ),
        )
        return [(identifier, [s.model_dump() for s in game.spans])]


def advance_source(db, source_id):
    with db.snapshot_session() as session:
        source = session.get(Source, source_id).document
    if any(w in source.get("title", "").lower() for w in db.settings.auto_lineup_exclude):
        raise ValueError("Automatic skills excluded by recording title filter")
    if not source.get("duration_seconds") or (
        source.get("live_status") in ("is_live", "is_upcoming")
        or (source.get("live_status") == "post_live" and not has_verified_download(source))
    ):
        raise ValueError("Skills analysis is waiting for a complete, seekable recording")
    config = load_vision_config()
    vision_ready = config.enabled and bool(config.api_key)
    with db.snapshot_session() as session:
        existing_game = any(
            not g.document.get("retired_into")
            and any(s["source_id"] == source_id for s in g.document["spans"])
            for g in session.scalars(select(GameRow))
        )
    if not vision_ready and not existing_game:
        raise ValueError(
            "Enable vision and configure its key in .env to analyze deliveries; no requests were sent"
        )
    games = prepare_games(db, source_id)
    for game_id, spans in games:
        for span in spans:
            if span["source_id"] != source_id or not vision_ready:
                continue
            start = span["start_seconds"]
            while start < span["end_seconds"] - 3:
                end = min(start + 1800, span["end_seconds"])
                start_campaign(
                    db,
                    game_id,
                    CampaignRequest(
                        source_id=source_id,
                        start_seconds=start,
                        end_seconds=end,
                        max_refinements=4,
                        max_call_candidates=1,
                    ),
                    automatic=True,
                )
                start = end
        # Local tracking follows available delivery proposals, even when a vision budget pauses the survey.
        with db.snapshot_session() as session:
            active = session.scalar(
                select(SkillMotionBatch.id).where(
                    SkillMotionBatch.game_id == game_id, SkillMotionBatch.state.in_(["active", "paused"])
                )
            )
        if not active:
            request = MotionBatchRequest(max_candidates=4)
            plan = preview_batch(db, game_id, request)
            if plan["plan"]["units"]:
                start_batch(db, game_id, MotionBatchStart(request=request, plan_sha256=plan["plan_sha256"]))
    return [g[0] for g in games]


def followup_status(session, game_ids):
    """Project actual child work rather than reporting every scheduled run as started."""
    campaigns = list(session.scalars(select(SkillCampaign).where(SkillCampaign.game_id.in_(game_ids))))
    motions = list(session.scalars(select(SkillMotionBatch).where(SkillMotionBatch.game_id.in_(game_ids))))
    children = campaigns + motions
    blockers = sorted({r.document["pause_reason"] for r in children if r.document.get("pause_reason")})
    gaps = sum(len(c.document.get("coverage_gaps", [])) for c in campaigns)
    state = (
        "running"
        if any(r.state == "active" for r in children)
        else "paused"
        if any(r.state == "paused" for r in children)
        else "cancelled"
        if any(r.state == "cancelled" for r in children)
        else "completed_with_gaps"
        if gaps
        else "completed"
        if children
        else "waiting"
    )
    return {"state": state, "game_ids": game_ids, "blockers": blockers, "coverage_gaps": gaps}


def advance_recording_followups(db, force=False):
    global _next_check
    if not db.settings.auto_skills:
        return
    if not force and time.monotonic() < _next_check:
        return
    _next_check = time.monotonic() + 30
    with db.snapshot_session() as session:
        batches = list(
            session.scalars(
                select(ProcessingBatch)
                .where(
                    ProcessingBatch.document["campaign_purpose"].as_string() == "recording_analysis",
                )
                .order_by(ProcessingBatch.document["created_at"].as_string().desc())
            )
        )
        latest = {}
        for batch in batches:
            latest.setdefault(batch.document["request"]["source_ids"][0], (batch.id, batch.state))
    for source_id, (batch_id, batch_state) in latest.items():
        if batch_state != "completed":
            continue
        try:
            game_ids = advance_source(db, source_id)
            with db.snapshot_session() as session:
                status = followup_status(session, game_ids)
            config = load_vision_config()
            if not config.enabled or not config.api_key:
                status["reason"] = (
                    "Vision is disabled; existing delivery proposals still receive local tracking"
                )
        except ValueError as exc:
            status = {"state": "blocked", "reason": str(exc)}
        with db.snapshot_session(write=True) as session:
            row = session.get(ProcessingBatch, batch_id)
            if row and row.document.get("skills_followup") != status:
                row.document = {**row.document, "skills_followup": status}


def advance_safely(db):
    try:
        advance_recording_followups(db)
    except Exception:
        log.exception("Recording follow-up scheduling will retry")
