import math
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest
from PIL import Image
from sqlalchemy import func, select

from hogline.db import Database, GameRow, Job, SkillCampaign, SkillSequence
from hogline.domain.catalog import Conflict
from hogline.orchestration import skill_campaigns as campaigns
from hogline.orchestration import skills, worker
from hogline.perception.vision_config import VisionConfig


@pytest.fixture
def excerpt(db, sources, monkeypatch):
    config = VisionConfig("secret", "gpt-6.1-terra", Decimal(5), True)
    monkeypatch.setattr(campaigns, "load_vision_config", lambda: config)
    monkeypatch.setattr(skills, "load_vision_config", lambda: config)
    monkeypatch.setattr(skills, "resolve_media", lambda source: {"uri": "fake:video"})
    with db.snapshot_session(write=True) as session:
        session.add(
            GameRow(
                id="game",
                document={"spans": [{"source_id": sources[0]["id"], "start_seconds": 0, "end_seconds": 100}]},
            )
        )

    def decode(uri, start, end, interval, **kwargs):
        for n in range(math.ceil((end - start) / interval)):
            yield (
                Image.new("RGB", (640, 360), (n * 10, 30, 40)),
                {"pts": round((start + n * interval) * 1000), "time_base_num": 1, "time_base_den": 1000},
            )

    monkeypatch.setattr(skills, "decode_window", decode)

    def vision(root, sheets, **kwargs):
        result = {
            "id": "fake-vision",
            "proposal": {
                "call": "unknown",
                "call_basis": "Not visible",
                "broom_visible": False,
                "delivery_visible": True,
                "sweeping_visible": False,
                "observations": [],
                "limitations": [],
                "deliveries": [
                    {
                        "first_frame": 1,
                        "last_frame": 3,
                        "release_frame": None,
                        "stone_color": "red",
                        "thrower_description": "Gray shirt",
                        "thrower_actor_tag": "A1",
                        "call": "unknown",
                        "call_basis": "Not visible",
                        "broom_frame": None,
                        "broom_tip": None,
                        "limitations": [],
                    }
                ],
                "activities": [
                    {
                        "actor_tag": "A1",
                        "description": "Gray shirt",
                        "first_frame": 1,
                        "last_frame": 3,
                        "key_frame": 1,
                        "bbox": [0.05, 0.1, 0.2, 0.7],
                        "action": "delivery",
                        "team_side": "red",
                        "side_basis": "Controls red stone",
                        "stone_color": "red",
                        "delivery_index": 0,
                        "evidence_basis": "Visible slide",
                        "limitations": [],
                    }
                ],
            },
        }
        if kwargs["schema"].__name__ == "CallEvidenceProposal":
            result["proposal"].update(
                targets=[], strategy_options=[], deliveries=[], activities=[], delivery_visible=False
            )
        return result

    monkeypatch.setattr(skills, "propose_call", vision)
    return campaigns.CampaignRequest(
        source_id=sources[0]["id"], start_seconds=0, end_seconds=60, max_refinements=2, max_call_candidates=0
    )


def view(db, identifier):
    with db.snapshot_session() as session:
        return campaigns.campaign_view(session, session.get(SkillCampaign, identifier))


def finish(db, identifier):
    for _ in range(20):
        worker.run_one(db)
        result = view(db, identifier)
        if result["state"] != "active":
            return result
    pytest.fail("Bounded excerpt did not finish")


def test_campaign_surveys_overlapping_windows_then_refines_and_reuses_jobs(db, excerpt):
    preview = campaigns.preview_campaign(db, "game", excerpt)
    assert preview["survey_clips"] == 3 and preview["maximum_refinement_clips"] == 2
    assert [(r["start_seconds"], r["end_seconds"]) for r in preview["plan"]["survey_requests"]] == [
        (0, 30),
        (26, 56),
        (52, 60),
    ]
    started = campaigns.start_campaign(db, "game", excerpt)
    assert campaigns.start_campaign(db, "game", excerpt)["id"] == started["id"]
    result = finish(db, started["id"])
    assert result["state"] == "completed", result["pause_reason"]
    assert len(result["survey"]) == 3 and len(result["refinements"]) == 2
    assert result["delivery_candidate_count"] == 3 and result["deferred_refinements"] == 1
    assert all(u["state"] == "succeeded" for u in result["survey"] + result["refinements"])
    assert all(
        u["request"]["view"] == "perspective_left" and u["request"]["interval_seconds"] == 0.5
        for u in result["refinements"]
    )
    assert campaigns.start_campaign(db, "game", excerpt)["state"] == "completed"
    with db.snapshot_session() as session:
        assert s_count(session, SkillSequence) == 5


def s_count(session, model):
    return session.scalar(select(func.count()).select_from(model))


def test_parallel_advancers_admit_one_paid_clip_globally(db, excerpt):
    campaigns.start_campaign(db, "game", excerpt)
    campaigns.start_campaign(db, "game", excerpt.model_copy(update={"start_seconds": 10, "end_seconds": 70}))
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: campaigns.advance_skill_campaigns(db), range(4)))
    with db.snapshot_session() as session:
        assert s_count(session, Job) == 1
        assert session.scalar(select(Job)).state == "queued"


def test_campaign_adds_both_pre_delivery_house_views_without_post_delivery_frames(db, excerpt):
    request = excerpt.model_copy(update={"max_call_candidates": 1})
    started = campaigns.start_campaign(db, "game", request)
    result = finish(db, started["id"])
    assert result["state"] == "completed", result["pause_reason"]
    calls = [u for u in result["refinements"] if u.get("purpose") == "pre_delivery_call"]
    assert len(calls) == 2
    assert {u["request"]["view"] for u in calls} == {"house_top", "house_bottom"}
    assert all(u["request"]["start_seconds"] == 0 and u["request"]["end_seconds"] == 2 for u in calls)
    assert result["deferred_refinements"] == 1


def test_pause_finishes_admitted_clip_and_resume_keeps_progress(db, excerpt):
    initial = campaigns.start_campaign(db, "game", excerpt)
    campaigns.advance_skill_campaigns(db)
    with pytest.raises(Conflict):
        campaigns.control_campaign(
            db,
            initial["id"],
            campaigns.CampaignControl(action="pause", expected_revision=initial["revision"]),
        )
    current = view(db, initial["id"])
    campaigns.control_campaign(
        db, current["id"], campaigns.CampaignControl(action="pause", expected_revision=current["revision"])
    )
    assert worker.run_one(db)["state"] == "succeeded"
    assert worker.run_one(db) is None
    paused = view(db, initial["id"])
    assert paused["survey"][0]["state"] == "succeeded" and paused["survey"][1]["state"] == "not_queued"
    campaigns.control_campaign(
        db, paused["id"], campaigns.CampaignControl(action="resume", expected_revision=paused["revision"])
    )
    assert finish(Database(db.settings), initial["id"])["state"] == "completed"


def test_budget_pauses_before_new_job_but_reuses_successful_evidence(db, excerpt, monkeypatch):
    excerpt = excerpt.model_copy(update={"end_seconds": 30, "max_refinements": 0})
    first = skills.SequenceRequest.model_validate(campaigns.survey_requests(excerpt)[0])
    skills.enqueue_sequence(db, "game", first)
    assert worker.run_one(db)["state"] == "succeeded"
    monkeypatch.setattr(
        campaigns, "load_vision_config", lambda: VisionConfig("secret", "gpt-6.1-terra", Decimal(".5"), True)
    )
    cached = campaigns.start_campaign(db, "game", excerpt)
    assert finish(db, cached["id"])["state"] == "completed"
    fresh = campaigns.start_campaign(
        db, "game", excerpt.model_copy(update={"start_seconds": 30, "end_seconds": 60})
    )
    campaigns.advance_skill_campaigns(db)
    assert view(db, fresh["id"])["state"] == "paused"
    assert "budget" in view(db, fresh["id"])["pause_reason"]
    with db.snapshot_session() as session:
        assert s_count(session, Job) == 1


def test_changed_game_or_failed_clip_pauses_without_automatic_retry(db, excerpt, monkeypatch):
    current = campaigns.start_campaign(db, "game", excerpt)
    monkeypatch.setattr(
        skills, "propose_call", lambda *a, **k: (_ for _ in ()).throw(ValueError("uncertain API outcome"))
    )
    assert worker.run_one(db)["state"] == "failed"
    campaigns.advance_skill_campaigns(db)
    paused = view(db, current["id"])
    assert paused["state"] == "paused" and "uncertain API outcome" in paused["pause_reason"]
    with db.snapshot_session() as session:
        assert session.scalar(select(Job)).attempts == 1
    with db.snapshot_session(write=True) as session:
        game = session.get(GameRow, "game")
        game.document = {**game.document, "note": "changed"}
    with pytest.raises(ValueError, match="Game changed"):
        campaigns.control_campaign(
            db, paused["id"], campaigns.CampaignControl(action="resume", expected_revision=paused["revision"])
        )


def test_campaign_rejects_outside_game_and_unbounded_requests(db, excerpt):
    with pytest.raises(ValueError, match="source span"):
        campaigns.start_campaign(db, "game", excerpt.model_copy(update={"end_seconds": 101}))
    with pytest.raises(ValueError, match="30 minutes"):
        campaigns.CampaignRequest(source_id="source", start_seconds=0, end_seconds=1801)


def test_excerpt_http_preview_progress_and_cancel(db, excerpt):
    from fastapi.testclient import TestClient

    from hogline.review.app import create_app

    client = TestClient(create_app(db.settings))
    path = "/api/games/game/skill-campaigns"
    assert client.post(path + "/preview", json=excerpt.model_dump()).json()["survey_clips"] == 3
    started = client.post(path, json=excerpt.model_dump())
    assert started.status_code == 200
    row = started.json()
    assert client.get(path).json()["campaigns"][0]["id"] == row["id"]
    cancelled = client.post(
        f"/api/skill-campaigns/{row['id']}/control",
        json={"action": "cancel", "expected_revision": row["revision"]},
    )
    assert cancelled.json()["state"] == "cancelled"
    campaigns.advance_skill_campaigns(db)
    with db.snapshot_session() as session:
        assert s_count(session, Job) == 0
