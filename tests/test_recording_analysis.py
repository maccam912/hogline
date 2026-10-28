import pytest
from fastapi.testclient import TestClient

from hogline.adapters.sources import import_youtube
from hogline.db import Database, Job, ProcessingBatch, Source
from hogline.orchestration import worker
from hogline.orchestration.plans import BatchControl, control_batch
from hogline.orchestration.recording import RecordingAnalysisRequest, start_recording_analysis
from hogline.review.app import create_app
from tests.test_automatic import media  # noqa: F401


def small_source(db, sources, duration=180):
    return import_youtube(
        db,
        "https://www.youtube.com/watch?v=CCCCCCCCCCC",
        {
            "id": "CCCCCCCCCCC",
            "title": "SHEET 5 | 01-16-26",
            "duration": duration,
            "upload_date": "20260117",
        },
    )["id"]


def drain(db):
    for _ in range(180):
        result = worker.run_one(db)
        if result is None:
            return
        assert result["state"] == "succeeded", result
    pytest.fail("Recording pipeline did not terminate")


def test_one_start_covers_whole_recording_and_repeated_clicks_reuse_live_analysis(db, sources):
    source_id = small_source(db, sources, 7455)
    first = start_recording_analysis(db, source_id, RecordingAnalysisRequest())
    assert first["request"]["start_seconds"] == 0 and first["request"]["end_seconds"] == 7455
    assert first["deferred_units"] == 0
    assert first["units"][0]["request"]["start_seconds"] == 0
    assert first["units"][-1]["request"]["end_seconds"] == 7455
    assert all(u["request"]["acquisition_tier"] == "evidence" for u in first["units"])
    again = start_recording_analysis(db, source_id, RecordingAnalysisRequest())
    assert again["id"] == first["id"] and again["already_exists"]
    assert first["progress_summary"]["overall_fraction"] == 0
    assert first["progress_summary"]["started_at"] is None


@pytest.mark.usefixtures("media")
def test_pipeline_restart_all_stages_and_result_checksum(db, sources):
    source_id = small_source(db, sources, 7200)
    first = start_recording_analysis(db, source_id, RecordingAnalysisRequest(detail_windows=2))
    assert worker.run_one(db)["state"] == "succeeded"
    drain(Database(db.settings))
    client = TestClient(create_app(db.settings))
    data = client.get("/api/catalog").json()
    batch = next(b for b in data["batches"] if b["id"] == first["id"])
    assert batch["state"] == "completed" and batch["phase"] == "reconstruct"
    assert batch["progress_summary"]["overall_fraction"] == 1
    assert batch["progress_summary"]["processing_elapsed_seconds"] > 0
    assert "analysis_result" not in batch
    assert len([p for p in batch["phase_history"] if p["phase"] == "boards"]) >= 2
    assert {"boards", "detail", "detail_boards", "reconstruct"} <= {
        p["phase"] for p in batch["phase_history"]
    }
    result = client.get(f"/api/analyses/{first['id']}/result").json()
    assert result["source_id"] == source_id and result["version"]
    assert result["board_ledgers"] == []  # Synthetic empty footage is not a completed game.
    assert not data["games"]
    assert data["worker"]["running"] is False
    assert all(j["execution"] for j in data["jobs"])
    with db.session() as session:
        row = session.get(ProcessingBatch, first["id"])
        row.document = {
            **row.document,
            "analysis_result": {**row.document["analysis_result"], "source_id": "wrong"},
        }
    assert client.get(f"/api/analyses/{first['id']}/result").status_code == 422


@pytest.mark.usefixtures("media")
def test_source_change_and_budget_pause_preserve_whole_recording_intent(db, sources):
    source_id = small_source(db, sources)
    first = start_recording_analysis(db, source_id, RecordingAnalysisRequest())
    assert worker.run_one(db)["state"] == "succeeded"
    with db.session() as session:
        row = session.get(ProcessingBatch, first["id"])
        row.document = {**row.document, "spent_worker_seconds": row.document["limits"]["max_worker_seconds"]}
    assert worker.run_one(db) is None
    with db.session() as session:
        row = session.get(ProcessingBatch, first["id"])
        assert row.state == "paused"
        assert row.document["request"]["end_seconds"] == 180
    control_batch(db, first["id"], BatchControl(action="resume", max_worker_seconds=1800))
    with db.session() as session:
        source = session.get(Source, source_id)
        source.document = {**source.document, "metadata_sha256": "changed"}
    assert worker.run_one(db) is None
    with db.session() as session:
        row = session.get(ProcessingBatch, first["id"])
        assert row.state == "paused" and "metadata changed" in row.document["pause_reason"]


def test_retry_and_resume_is_atomic_and_retains_failed_attempt_history(db, sources):
    source_id = small_source(db, sources, 2000)
    first = start_recording_analysis(db, source_id, RecordingAnalysisRequest())
    ids = [u["job_id"] for u in first["units"]]
    with db.session() as session:
        row = session.get(ProcessingBatch, first["id"])
        row.state = "paused"
        for i, identifier in enumerate(ids):
            job = session.get(Job, identifier)
            job.state, job.attempts = "failed", 1 if i == 0 else 3
    with pytest.raises(ValueError, match="retry limit"):
        control_batch(db, first["id"], BatchControl(action="resume", retry_failed=True))
    with db.session() as session:
        assert all(session.get(Job, identifier).state == "failed" for identifier in ids)
        job = session.get(Job, ids[1])
        job.attempts = 1
    resumed = control_batch(db, first["id"], BatchControl(action="resume", retry_failed=True))
    assert resumed["state"] == "active" and resumed["progress_summary"]["queued_jobs"] == 2
    with db.session() as session:
        assert all(session.get(Job, identifier).attempts == 1 for identifier in ids)


def test_recording_api_refuses_unknown_duration_and_returns_conflict_until_ready(db, sources):
    source_id = small_source(db, sources)
    client = TestClient(create_app(db.settings))
    first = client.post(f"/api/recordings/{source_id}/analyze", json={}).json()
    assert client.get(f"/api/analyses/{first['id']}/result").status_code == 409
    assert client.post(f"/api/recordings/{source_id}/analyze", json={}).json()["already_exists"]
    with db.session() as session:
        row = session.get(Source, sources[1]["id"])
        row.document = {**row.document, "duration_seconds": None}
    assert client.post(f"/api/recordings/{sources[1]['id']}/analyze", json={}).status_code == 422
