import math
from concurrent.futures import ThreadPoolExecutor

import pytest
from PIL import Image
from sqlalchemy import select

from hogline.adapters.sources import digest
from hogline.config import profile
from hogline.db import Database, GameRow, Job, SkillMotionBatch, SkillSequence
from hogline.domain.catalog import Conflict
from hogline.domain.skills import Shot, SkillDocument, SkillEdit, save_skills
from hogline.orchestration import skill_motion as motion
from hogline.orchestration import skill_motion_batches as batches
from hogline.orchestration import worker
from hogline.orchestration.skill_campaigns import CampaignControl


@pytest.fixture
def deliveries(db, sources, monkeypatch):
    source = sources[0]
    with db.snapshot_session(write=True) as session:
        session.add(
            GameRow(
                id="game",
                document={"spans": [{"source_id": source["id"], "start_seconds": 0, "end_seconds": 100}]},
            )
        )
        session.flush()
        for start in (10, 30, 50):
            job = Job(job_key=f"sequence-{start}", document={}, state="succeeded")
            session.add(job)
            session.flush()
            doc = {
                "id": f"sequence-{start}",
                "game_revision": 1,
                "source_id": source["id"],
                "source_metadata_sha256": source["metadata_sha256"],
                "profile_sha256": digest(profile()),
                "created_at": "2026-10-30",
                "request": {"view": "perspective_left"},
                "samples": [{"source_seconds": start + i} for i in range(3)],
                "proposal": {
                    "deliveries": [
                        {
                            "first_frame": 0,
                            "last_frame": 2,
                            "release_frame": 2,
                            "stone_color": "red",
                            "call": "unknown",
                            "thrower_description": "Visible delivery",
                            "thrower_actor_tag": None,
                        }
                    ]
                },
            }
            session.add(
                SkillSequence(id=doc["id"], game_id="game", job_id=job.id, document=doc, sha256=digest(doc))
            )
    monkeypatch.setattr(motion, "resolve_media", lambda source: {"uri": "fake:video"})
    monkeypatch.setattr(motion, "optional_person_detector", lambda root: (None, {"status": "fixture"}))

    def decode(uri, start, end, interval, max_frames, **kwargs):
        for i in range(min(max_frames, math.ceil((end - start) / interval))):
            yield (
                Image.new("RGB", (640, 360), "white"),
                {"pts": round((start + i * interval) * 1000), "time_base_num": 1, "time_base_den": 1000},
            )

    monkeypatch.setattr(motion, "decode_window", decode)
    return source


def start(db, limit=2):
    request = batches.MotionBatchRequest(max_candidates=limit)
    preview = batches.preview_batch(db, "game", request)
    payload = batches.MotionBatchStart(request=request, plan_sha256=preview["plan_sha256"])
    return batches.start_batch(db, "game", payload), payload


def view(db, identifier):
    with db.snapshot_session() as session:
        return batches.batch_view(session, session.get(SkillMotionBatch, identifier))


def motion_jobs(db):
    with db.snapshot_session() as session:
        return list(session.scalars(select(Job).where(Job.document["kind"].as_string() == "skill_motion")))


def test_bounded_local_batch_survives_restart_and_reuses_completed_evidence(db, deliveries, monkeypatch):
    # No vision key, model configuration or API call is required by the local scheduler.
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    result, payload = start(db)
    assert len(result["units"]) == 2
    assert result["plan"]["maximum_decoded_samples"] == 122  # Includes one reference frame per clip.
    assert result["plan"]["omitted"][0]["reason"] == "beyond_selected_limit"
    assert batches.start_batch(db, "game", payload)["id"] == result["id"]
    assert worker.run_one(db)["state"] == "succeeded"
    reopened = Database(db.settings)
    assert worker.run_one(reopened)["state"] == "succeeded"
    batches.advance_motion_batches(reopened)
    assert view(reopened, result["id"])["state"] == "completed"
    assert len(motion_jobs(db)) == 2
    assert batches.start_batch(db, "game", payload)["state"] == "completed"
    remaining = batches.preview_batch(db, "game", batches.MotionBatchRequest())["plan"]
    assert len(remaining["units"]) == 1
    assert [u["reason"] for u in remaining["omitted"]] == ["current_motion_already_available"] * 2
    assert all(u["motion_ids"] for u in remaining["omitted"])


def test_concurrent_advancers_admit_one_motion_and_respect_manual_queue(db, deliveries):
    first, _ = start(db, 2)
    second, _ = start(db, 3)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: batches.advance_motion_batches(db), range(4)))
    assert len(motion_jobs(db)) == 1
    assert sum(bool(u["job_id"]) for b in (first, second) for u in view(db, b["id"])["units"]) == 1
    batches.advance_motion_batches(db)
    assert len(motion_jobs(db)) == 1


def test_pause_cancel_and_stale_control_do_not_admit_future_clips(db, deliveries):
    result, _ = start(db)
    batches.advance_motion_batches(db)
    result = view(db, result["id"])
    paused = batches.control_batch(
        db, result["id"], CampaignControl(action="pause", expected_revision=result["revision"])
    )
    with pytest.raises(Conflict):
        batches.control_batch(
            db, result["id"], CampaignControl(action="resume", expected_revision=result["revision"])
        )
    assert worker.run_one(db)["state"] == "succeeded"  # already admitted work may finish
    batches.advance_motion_batches(db)
    assert len(motion_jobs(db)) == 1
    resumed = batches.control_batch(
        db, result["id"], CampaignControl(action="resume", expected_revision=paused["revision"])
    )
    batches.control_batch(
        db, result["id"], CampaignControl(action="cancel", expected_revision=resumed["revision"])
    )
    batches.advance_motion_batches(db)
    assert len(motion_jobs(db)) == 1
    assert view(db, result["id"])["state"] == "cancelled"


def test_new_exclusion_invalidates_preview_and_pauses_pending_batch(db, deliveries):
    result, _ = start(db, 1)
    preview = batches.preview_batch(db, "game", batches.MotionBatchRequest(max_candidates=2))
    edit = SkillEdit(
        expected_revision=0,
        document=SkillDocument(
            shots=[
                Shot(
                    id="excluded",
                    source_id=deliveries["id"],
                    seconds=12,
                    side="red",
                    acceptance="excluded",
                    evidence=["skill-sequence:sequence-10:frame:0"],
                )
            ]
        ),
    )
    save_skills(db, "game", edit)
    with pytest.raises(Conflict):
        batches.start_batch(
            db,
            "game",
            batches.MotionBatchStart(
                request=batches.MotionBatchRequest(max_candidates=2), plan_sha256=preview["plan_sha256"]
            ),
        )
    batches.advance_motion_batches(db)
    assert view(db, result["id"])["state"] == "paused"
    assert not motion_jobs(db)
    fresh = batches.preview_batch(db, "game", batches.MotionBatchRequest())["plan"]
    assert len(fresh["units"]) == 2
    assert fresh["omitted"][0]["reason"] == "excluded_by_review"


def test_changed_anchor_and_failed_job_stop_without_automatic_retry(db, deliveries):
    result, _ = start(db)
    batches.advance_motion_batches(db)
    job = motion_jobs(db)[0]
    with db.snapshot_session(write=True) as session:
        session.get(Job, job.id).state = "failed"
    batches.advance_motion_batches(db)
    assert view(db, result["id"])["state"] == "paused"
    assert len(motion_jobs(db)) == 1
    # A separate plan is also frozen to its exact sequence observations.
    other, _ = start(db, 3)
    with db.snapshot_session(write=True) as session:
        row = session.get(SkillSequence, "sequence-10")
        row.document = {**row.document, "changed": True}
        row.sha256 = digest(row.document)
    batches.advance_motion_batches(db)
    assert view(db, other["id"])["state"] == "paused"
    assert "anchor" in view(db, other["id"])["pause_reason"]


def test_preview_and_start_routes_are_bounded_and_revision_safe(db, deliveries):
    from fastapi.testclient import TestClient

    from hogline.review.app import create_app

    client = TestClient(create_app(db.settings))
    base = "/api/games/game/skill-motion-batches"
    assert client.post(base + "/preview", json={"max_candidates": 13}).status_code == 422
    preview = client.post(base + "/preview", json={"max_candidates": 1}).json()
    started = client.post(
        base, json={"request": {"max_candidates": 1}, "plan_sha256": preview["plan_sha256"]}
    )
    assert started.status_code == 200, started.text
    assert len(client.get(base).json()["batches"]) == 1
