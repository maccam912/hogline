"""Skill review endpoints."""

import json

from fastapi.responses import Response
from sqlalchemy import select

from hogline.adapters.sources import digest
from hogline.analytics.skills import skill_report
from hogline.db import GameRow, Job, SkillMotion, SkillSequence, Source
from hogline.domain.skill_calibration import CameraCalibration
from hogline.domain.skill_scope import CoverageExtension, extend_skill_coverage
from hogline.domain.skills import (
    Shot,
    SkillDocument,
    SkillEdit,
    read_skills,
    role_proposals,
    save_skills,
    skill_history,
)
from hogline.media.frames import read_image_bytes
from hogline.orchestration.skill_calibration import (
    aim_preview,
    calibration_draft,
    calibration_preview,
    calibration_reports,
)
from hogline.orchestration.skill_camera_transfer import (
    CalibrationTransferRequest,
    CameraComparisonRequest,
    compare_cameras,
    transfer_calibration,
)
from hogline.orchestration.skill_campaigns import (
    CampaignControl,
    CampaignRequest,
    campaigns_in_session,
    control_campaign,
    preview_campaign,
    start_campaign,
)
from hogline.orchestration.skill_house_context import (
    analyze_house_context,
    house_context_image,
    latest_house_context,
)
from hogline.orchestration.skill_motion import (
    MotionRequest,
    enqueue_candidate_motion,
    enqueue_motion,
    motions_in_session,
)
from hogline.orchestration.skill_motion_batches import (
    MotionBatchRequest,
    MotionBatchStart,
    batches_in_session,
    control_batch,
    preview_batch,
    start_batch,
)
from hogline.orchestration.skill_motion_refinement import refine_motion
from hogline.orchestration.skill_order import enqueue_gap_search, gap_search_plan
from hogline.orchestration.skill_segmentation import (
    DirectionRuleEdit,
    direction_rule,
    hierarchy_in_session,
    save_direction_rule,
)
from hogline.orchestration.skills import (
    SequenceRequest,
    enqueue_candidate_calls,
    enqueue_sequence,
    sequences_in_session,
)
from hogline.perception.shot_motion import VERSION as MOTION_TRACKING_VERSION
from hogline.perception.vision_budget import budget_status
from hogline.perception.vision_config import load_vision_config
from hogline.reconstruction.skill_actors import actor_proposals
from hogline.reconstruction.skill_calls import call_evidence
from hogline.reconstruction.skill_candidates import delivery_candidates
from hogline.reconstruction.skill_continuity import continuity_proposals
from hogline.reconstruction.skill_drafts import candidate_coverage, candidate_review
from hogline.reconstruction.skill_order import order_report


def register_skills(app, db):
    @app.get("/api/skills/end-direction")
    def read_end_direction():
        with db.snapshot_session() as session:
            return direction_rule(session)

    @app.put("/api/skills/end-direction")
    def update_end_direction(edit: DirectionRuleEdit):
        return save_direction_rule(db, edit)

    @app.get("/api/games/{game_id}/skill-segments")
    def read_segment_hierarchy(game_id: str):
        with db.snapshot_session() as session:
            return hierarchy_in_session(session, game_id)

    @app.post("/api/games/{game_id}/skill-motion/{motion_id}/camera-comparison")
    def compare_camera_frames(game_id: str, motion_id: str, request: CameraComparisonRequest):
        return compare_cameras(db, game_id, motion_id, request)

    @app.post("/api/games/{game_id}/skills/calibration-transfer")
    def reuse_camera_map(game_id: str, request: CalibrationTransferRequest):
        return transfer_calibration(db, game_id, request)

    @app.post("/api/games/{game_id}/skill-motion-batches/preview")
    def preview_local_motion(game_id: str, request: MotionBatchRequest):
        return preview_batch(db, game_id, request)

    @app.post("/api/games/{game_id}/skill-motion-batches")
    def start_local_motion(game_id: str, request: MotionBatchStart):
        return start_batch(db, game_id, request)

    @app.get("/api/games/{game_id}/skill-motion-batches")
    def local_motion_batches(game_id: str):
        with db.snapshot_session() as session:
            return {"batches": batches_in_session(session, game_id)}

    @app.post("/api/skill-motion-batches/{batch_id}/control")
    def control_local_motion(batch_id: str, control: CampaignControl):
        return control_batch(db, batch_id, control)

    @app.post("/api/skill-motion/{motion_id}/refine")
    def refine_retained_motion(motion_id: str):
        return refine_motion(db, motion_id)

    @app.post("/api/games/{game_id}/skills/aim-preview")
    def preview_aim(game_id: str, shot: Shot):
        with db.snapshot_session() as session:
            current = read_skills(session, game_id)
        return aim_preview(
            db, game_id, SkillDocument.model_validate(current["document"]), shot, current["stale"]
        )

    @app.get("/api/games/{game_id}/skill-motion/{motion_id}/calibration-draft")
    def camera_calibration_draft(game_id: str, motion_id: str):
        return calibration_draft(db, game_id, motion_id)

    @app.post("/api/games/{game_id}/skills/calibration-preview")
    def preview_calibration(game_id: str, calibration: CameraCalibration):
        return calibration_preview(db, game_id, calibration)

    @app.post("/api/games/{game_id}/skill-candidates/{candidate_id}/motion")
    def candidate_motion(game_id: str, candidate_id: str):
        return enqueue_candidate_motion(db, game_id, candidate_id)

    @app.post("/api/games/{game_id}/skills/order-gaps/{lead_id}/search")
    def search_order_gap(game_id: str, lead_id: str):
        return enqueue_gap_search(db, game_id, lead_id)

    @app.post("/api/games/{game_id}/skills/house-context")
    def analyze_houses(game_id: str):
        return analyze_house_context(db, game_id)

    @app.get("/api/skill-house-context/{context_id}/{group_index}/{state_index}/{endpoint}.png")
    def context_image(context_id: str, group_index: int, state_index: int, endpoint: str):
        return Response(
            house_context_image(db, context_id, group_index, state_index, endpoint), media_type="image/png"
        )

    @app.post("/api/games/{game_id}/skills/coverage")
    def extend_coverage(game_id: str, request: CoverageExtension):
        return extend_skill_coverage(db, game_id, request)

    @app.get("/api/skills/vision-proposals")
    def vision_proposals():
        root = db.settings.data_dir / "skill-vision"
        return {"proposals": [json.loads(p.read_text()) for p in sorted(root.glob("*.json"))]}

    @app.get("/api/skills/catalog")
    def catalog():
        with db.snapshot_session() as session:
            games = [
                r.document for r in session.scalars(select(GameRow)) if not r.document.get("retired_into")
            ]
            sources = [
                {
                    "id": r.id,
                    **{
                        k: r.document.get(k)
                        for k in ("title", "sheet", "duration_seconds", "manual_preview_label")
                    },
                }
                for r in session.scalars(select(Source))
            ]
        sources.sort(
            key=lambda s: (
                s.get("manual_preview_label", {}).get("decision") != "team_visible"
                if s.get("manual_preview_label")
                else True,
                s.get("title") or "",
            )
        )
        return {"games": games, "sources": sources}

    @app.get("/api/skills/vision-status")
    def vision_status():
        return {
            **load_vision_config().public_status(),
            **budget_status(db.settings.data_dir / "skill-vision"),
        }

    @app.post("/api/games/{game_id}/skill-sequences")
    def enqueue(game_id: str, request: SequenceRequest):
        return enqueue_sequence(db, game_id, request)

    @app.post("/api/games/{game_id}/skill-candidates/{candidate_id}/call-evidence")
    def find_call(game_id: str, candidate_id: str):
        return enqueue_candidate_calls(db, game_id, candidate_id)

    @app.get("/api/skill-sequences/{sequence_id}/targets/{index}/{kind}.png")
    def target_image(sequence_id: str, index: int, kind: str):
        if kind not in ("native", "overlay"):
            raise ValueError("Choose native or overlay target evidence")
        with db.snapshot_session() as session:
            row = session.get(SkillSequence, sequence_id)
            if row is None or digest(row.document) != row.sha256:
                raise ValueError("Target evidence not found or changed")
            targets = row.document.get("target_assets", [])
            if not 0 <= index < len(targets):
                raise ValueError("Target evidence unavailable")
            asset = targets[index][kind + "_asset"]
        return Response(read_image_bytes(db.settings.data_dir, asset), media_type="image/png")

    @app.post("/api/games/{game_id}/skill-campaigns/preview")
    def preview_excerpt(game_id: str, request: CampaignRequest):
        return preview_campaign(db, game_id, request)

    @app.get("/api/games/{game_id}/skill-campaigns")
    def read_excerpts(game_id: str):
        with db.snapshot_session() as session:
            return {"campaigns": campaigns_in_session(session, game_id)}

    @app.post("/api/games/{game_id}/skill-campaigns")
    def start_excerpt(game_id: str, request: CampaignRequest):
        return start_campaign(db, game_id, request)

    @app.post("/api/skill-campaigns/{campaign_id}/control")
    def control_excerpt(campaign_id: str, control: CampaignControl):
        return control_campaign(db, campaign_id, control)

    @app.get("/api/skill-sequences/{sequence_id}/sheets/{index}.png")
    def sheet(sequence_id: str, index: int):
        with db.snapshot_session() as session:
            row = session.get(SkillSequence, sequence_id)
            if row is None or digest(row.document) != row.sha256:
                raise ValueError("Sequence not found or changed")
            assets = row.document["sheet_assets"]
            if not 0 <= index < len(assets):
                raise ValueError("Sequence sheet not found")
            asset = assets[index]
        return Response(read_image_bytes(db.settings.data_dir, asset), media_type="image/png")

    @app.post("/api/games/{game_id}/skill-motion")
    def motion(game_id: str, request: MotionRequest):
        return enqueue_motion(db, game_id, request)

    @app.get("/api/skill-sequences/{sequence_id}/actors/{index}.png")
    def actor_image(sequence_id: str, index: int):
        with db.snapshot_session() as session:
            row = session.get(SkillSequence, sequence_id)
            if row is None or digest(row.document) != row.sha256:
                raise ValueError("Sequence not found or changed")
            matches = [
                entry["asset"]
                for entry in row.document.get("activity_assets", [])
                if entry["activity_index"] == index and "asset" in entry
            ]
            if len(matches) != 1:
                raise ValueError("Actor evidence crop unavailable")
            asset = matches[0]
        return Response(read_image_bytes(db.settings.data_dir, asset), media_type="image/png")

    @app.get("/api/skill-motion/{motion_id}/images/{name}.png")
    def motion_image(motion_id: str, name: str):
        with db.snapshot_session() as session:
            row = session.get(SkillMotion, motion_id)
            if row is None or digest(row.document) != row.sha256:
                raise ValueError("Motion evidence not found or changed")
            if name in ("reference", "overlay"):
                asset = row.document[f"{name}_asset"]
            else:
                matches = [
                    r["asset"] for r in row.document["retained_samples"] if str(r["sample_index"]) == name
                ]
                if len(matches) != 1:
                    raise ValueError("Motion frame not retained")
                asset = matches[0]
        return Response(read_image_bytes(db.settings.data_dir, asset), media_type="image/png")

    @app.get("/api/games/{game_id}/skills")
    def read(game_id: str):
        with db.snapshot_session() as session:
            result = read_skills(session, game_id)
            sequences = sequences_in_session(session, game_id)
            motions = motions_in_session(session, game_id)
            campaigns = campaigns_in_session(session, game_id)
            motion_batches = batches_in_session(session, game_id)
            house_context = latest_house_context(session, game_id)
            jobs = [
                {
                    "id": r.id,
                    "state": r.state,
                    "error": r.document.get("error"),
                    "progress": r.document.get("progress"),
                    "request": r.document["request"],
                    "kind": r.document["kind"],
                    "processing_mode": r.document.get("processing_mode"),
                }
                for r in session.scalars(
                    select(Job).where(
                        Job.document["kind"].as_string().in_(["skill_sequence", "skill_motion"]),
                        Job.document["game_id"].as_string() == game_id,
                    )
                )
            ]
        document = SkillDocument.model_validate(result["document"])
        candidates = delivery_candidates(sequences, document.shots)
        for candidate in candidates:
            candidate["review"] = candidate_review(candidate, document.shots)
        confirmed = {shot.id for shot in document.shots if shot.acceptance == "confirmed"}
        duplicates = {
            identifier
            for candidate in candidates
            if len(set(candidate["linked_shot_ids"]) & confirmed) > 1
            for identifier in candidate["linked_shot_ids"]
            if identifier in confirmed
        }
        report = skill_report(document, duplicates)
        order = order_report(candidates, document, result["stale"])
        with db.snapshot_session() as session:
            for lead in order["search_leads"]:
                lead["search_plan"] = None if result["stale"] else gap_search_plan(session, lead, sequences)
        if result["stale"]:
            report = {
                **skill_report(SkillDocument()),
                "total_shots": len(document.shots),
                "excluded": {"stale_game_or_source": len(document.shots)},
            }
        return {
            **result,
            "role_proposals": role_proposals(document),
            "report": report,
            "sequences": sequences,
            "sequence_jobs": [j for j in jobs if j["kind"] == "skill_sequence"],
            "motion_jobs": [j for j in jobs if j["kind"] == "skill_motion"],
            "motions": motions,
            "motion_tracking_version": MOTION_TRACKING_VERSION,
            "motion_batches": motion_batches,
            "delivery_candidates": candidates,
            "candidate_coverage": candidate_coverage(candidates),
            "call_evidence": call_evidence(sequences, candidates),
            "actor_proposals": actor_proposals(sequences),
            "continuity_proposals": continuity_proposals(sequences, candidates, document),
            "campaigns": campaigns,
            "house_context": house_context,
            "order_report": order,
            "calibration_reports": calibration_reports(db, game_id, document.calibrations, result["stale"]),
        }

    @app.put("/api/games/{game_id}/skills")
    def save(game_id: str, edit: SkillEdit):
        return save_skills(db, game_id, edit)

    @app.get("/api/games/{game_id}/skills/history")
    def history(game_id: str):
        with db.snapshot_session() as session:
            return skill_history(session, game_id)
