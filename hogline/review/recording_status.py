"""Explain independent score, face and skill stages in the recording workspace."""

from collections import Counter

from sqlalchemy import select

from hogline.analytics.ice_reviews import source_score_history
from hogline.config import profile
from hogline.db import (
    AutoLineup,
    FrameObservation,
    GameLineup,
    GameRow,
    ProcessingBatch,
    SkillCampaign,
    SkillMotion,
    SkillSequence,
)
from hogline.domain.catalog import source_or_error
from hogline.orchestration.auto_lineups import models_problem
from hogline.perception.broadcast import broadcast_context


def recording_status(db, source_id):
    # Reviews can add unposted ends after the immutable processing report finished.
    current_scores = source_score_history(db, source_id)
    live_summary = current_scores["summary"]
    with db.snapshot_session() as session:
        source_or_error(session, source_id)
        batch = session.scalar(
            select(ProcessingBatch)
            .where(
                ProcessingBatch.document["campaign_purpose"].as_string() == "recording_analysis",
                ProcessingBatch.document["request"]["source_ids"][0].as_string() == source_id,
            )
            .order_by(ProcessingBatch.document["created_at"].as_string().desc())
        )
        summary = batch.document.get("result_summary", {}) if batch else {}
        blockers = Counter()
        try:
            version = broadcast_context(session, db.settings.data_dir, profile())["version"]
            for row in session.scalars(
                select(FrameObservation).where(
                    FrameObservation.model_version == version,
                    FrameObservation.document["source_id"].as_string() == source_id,
                )
            ):
                board = row.document.get("scoreboard", {})
                if board.get("missing_reason"):
                    blockers[board["missing_reason"]] += 1
        except ValueError as exc:
            blockers[str(exc)] += 1
        faces = session.get(AutoLineup, source_id)
        lineups = list(session.scalars(select(GameLineup).where(GameLineup.source_id == source_id)))
        games = [
            g
            for g in session.scalars(select(GameRow))
            if not g.document.get("retired_into")
            and any(s["source_id"] == source_id for s in g.document["spans"])
        ]
        skills = []
        for game in games:
            campaigns = list(session.scalars(select(SkillCampaign).where(SkillCampaign.game_id == game.id)))
            sequences = list(session.scalars(select(SkillSequence).where(SkillSequence.game_id == game.id)))
            motions = list(session.scalars(select(SkillMotion).where(SkillMotion.game_id == game.id)))
            skills.append(
                {
                    "game_id": game.id,
                    "label": game.document["label"],
                    "campaigns": dict(Counter(c.state for c in campaigns)),
                    "blockers": sorted(
                        {c.document.get("pause_reason") for c in campaigns if c.document.get("pause_reason")}
                    ),
                    "sequences": len(sequences),
                    "motion_clips": len(motions),
                    "coverage_gaps": sum(len(c.document.get("coverage_gaps", [])) for c in campaigns),
                    "delivery_proposals": sum(
                        len(s.document.get("proposal", {}).get("deliveries", [])) for s in sequences
                    ),
                }
            )
        return {
            "scores": {
                "state": batch.state if batch else "not_started",
                **summary,
                "known_end_scores": live_summary["known_end_scores"],
                "proposed_complete_games": live_summary["proposed_complete_games"],
                "candidate_games": len(current_scores["histories"]),
                "status": "complete_results"
                if current_scores["histories"]
                and live_summary["proposed_complete_games"] == len(current_scores["histories"])
                else "scores_extracted"
                if live_summary["known_end_scores"]
                else summary.get("status", "no_scores"),
                "blockers": dict(blockers),
                "problem": batch.document.get("pause_reason") if batch else None,
            },
            "faces": {
                "state": faces.state if faces else "not_started",
                "lineups": len(lineups),
                "blocker": models_problem(db.settings.data_dir)
                or (faces.document.get("problem") if faces else None),
            },
            "skills": skills,
            "skills_followup": batch.document.get("skills_followup") if batch else None,
            "review_required": (
                "Skill ratings require reviewed player assignments, calls and camera calibration. "
                "Automated delivery and motion evidence stays provisional."
            ),
        }
