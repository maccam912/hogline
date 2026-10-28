from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import func, select

from hogline.db import Job, JobExecution, ProcessingBatch, Source
from hogline.domain.catalog import catalog
from hogline.orchestration import worker
from hogline.orchestration.plans import (
    BatchControl,
    PlanRequest,
    WorkStopped,
    batches,
    control_batch,
    create_batch,
    preview_plan,
)
from hogline.review.app import create_app


@pytest.fixture
def media(monkeypatch):
    calls = []
    monkeypatch.setattr(worker, "resolve_media", lambda source, max_height=1080: {"uri": "fixture"})

    def capture(_, seconds):
        calls.append(seconds)
        return Image.new("RGB", (640, 360), (int(seconds) % 255, 110, 80)), {
            "pts": int(seconds * 1000),
            "time_base_num": 1,
            "time_base_den": 1000,
        }

    monkeypatch.setattr(worker, "capture", capture)
    return calls


def request(source, **changes):
    return PlanRequest(source_ids=[source["id"]], **changes)


def test_dry_run_admits_bounded_chunks_without_jobs_or_media(db, sources):
    plan = preview_plan(db, request(sources[0], max_jobs=2))
    assert plan["total_requested_units"] == 60
    assert plan["admitted_new_units"] == 2
    assert plan["deferred_units"] == 58
    first, second = [u["payload"]["request"] for u in plan["units"]]
    assert (first["start_seconds"], first["end_seconds"]) == (0, 630)
    assert (second["start_seconds"], second["ownership_start_seconds"]) == (570, 600)
    assert first["ownership_end_seconds"] == 600
    assert first["propose_games"] is False
    with db.session() as session:
        assert session.scalar(select(func.count()).select_from(Job)) == 0
    assert not (db.settings.data_dir / "evidence").exists()
    with pytest.raises(ValueError, match="multiples"):
        request(sources[0], context_seconds=25)


def test_batch_joins_owned_coverage_reuses_context_and_advances_next_plan(db, sources, media):
    req = request(
        sources[0], end_seconds=180, chunk_seconds=60, context_seconds=30, interval_seconds=30, max_jobs=2
    )
    batch = create_batch(db, req)
    assert batch["deferred_units"] == 1
    assert worker.run_one(db)["state"] == "succeeded"
    assert worker.run_one(db)["state"] == "succeeded"
    done = batches(db)[0]
    assert done["state"] == "completed"
    assert done["reserved_image_bytes"] > 0
    assert len(done["executions"]) == 2
    assert len(media) == 5  # adjacent context reuses targets 30 and 60
    assert done["reserved_image_bytes"] == sum(e["new_image_bytes_reserved"] for e in done["executions"])
    data = catalog(db)
    assert data["games"] == []
    assert data["timelines"][0]["sparse_scanned_intervals"] == [[0, 120]]
    assert data["timelines"][0]["unique_owned_samples"] == 4
    remaining = preview_plan(db, req)
    assert remaining["reused_succeeded_units"] == 2
    assert remaining["admitted_new_units"] == 1
    assert remaining["deferred_units"] == 0


def test_operator_pause_and_cancel_preserve_execution_history(db, sources, media):
    req = request(sources[0], end_seconds=120, chunk_seconds=60, context_seconds=0, interval_seconds=30)
    batch = create_batch(db, req)
    control_batch(db, batch["id"], BatchControl(action="pause"))
    assert worker.run_one(db) is None
    assert not media
    control_batch(db, batch["id"], BatchControl(action="resume"))
    assert worker.run_one(db)["state"] == "succeeded"
    control_batch(db, batch["id"], BatchControl(action="cancel"))
    assert worker.run_one(db) is None
    assert set(u["state"] for u in batches(db)[0]["units"]) == {"succeeded", "cancelled"}
    replacement = create_batch(db, req)
    assert replacement["id"] != batch["id"]
    assert worker.run_one(db)["state"] == "succeeded"
    assert batches(db)[0]["state"] == "cancelled"
    assert set(u["state"] for u in batches(db)[0]["units"]) == {"succeeded", "cancelled"}
    assert batches(db)[1]["state"] == "completed"


def test_growth_budget_stops_before_write_and_resumes_original(db, sources, media, monkeypatch):
    calls = []

    def random_frame(_, seconds):
        calls.append(seconds)
        image = Image.fromarray(
            np.random.default_rng(int(seconds)).integers(0, 256, (360, 640, 3), dtype=np.uint8)
        )
        return image, {"pts": int(seconds * 1000), "time_base_num": 1, "time_base_den": 1000}

    monkeypatch.setattr(worker, "capture", random_frame)
    batch = create_batch(
        db,
        request(
            sources[0],
            end_seconds=60,
            chunk_seconds=60,
            context_seconds=0,
            interval_seconds=30,
            max_image_growth_mb=1,
        ),
    )
    assert worker.run_one(db)["state"] == "queued"
    paused = batches(db)[0]
    assert paused["state"] == "paused"
    assert "image-growth" in paused["pause_reason"]
    assert 0 < paused["reserved_image_bytes"] <= 1024**2
    assert calls == [0]
    assert paused["executions"][0]["state"] == "stopped"
    with db.session() as session:
        assert session.get(Job, batch["units"][0]["job_id"]).attempts == 0
    control_batch(db, batch["id"], BatchControl(action="resume", max_image_growth_mb=4))
    resumed = batches(db)[0]
    assert resumed["request"]["max_image_growth_mb"] == 1  # original preview remains immutable
    assert resumed["limits"]["max_image_growth_mb"] == 4
    assert resumed["control_history"][-1]["action"] == "resume"
    assert worker.run_one(db)["state"] == "succeeded"
    assert calls == [0, 30]
    assert batches(db)[0]["state"] == "completed"


def test_time_budget_and_mid_decode_cancel_never_publish(db, sources, media, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(worker.time, "monotonic", lambda: clock[0])
    req = request(
        sources[0],
        end_seconds=60,
        chunk_seconds=60,
        context_seconds=0,
        interval_seconds=30,
        max_worker_seconds=5,
    )
    batch = create_batch(db, req)
    original = worker.capture

    def delayed(*args):
        clock[0] += 6
        return original(*args)

    monkeypatch.setattr(worker, "capture", delayed)
    assert worker.run_one(db)["state"] == "queued"
    paused = batches(db)[0]
    assert paused["spent_worker_seconds"] == 6
    assert paused["state"] == "paused"
    assert not catalog(db)["scans"]
    with pytest.raises(ValueError, match="Raise the worker-time"):
        control_batch(db, batch["id"], BatchControl(action="resume"))
    control_batch(db, batch["id"], BatchControl(action="resume", max_worker_seconds=100))

    def cancel_mid_decode(*args):
        control_batch(db, batch["id"], BatchControl(action="cancel"))
        return original(*args)

    monkeypatch.setattr(worker, "capture", cancel_mid_decode)
    assert worker.run_one(db)["state"] == "cancelled"
    assert not catalog(db)["scans"]


def test_one_active_unit_per_batch_and_expired_lease_cannot_publish(db, sources, media):
    batch = create_batch(db, request(sources[0], end_seconds=120, chunk_seconds=60, context_seconds=0))
    first = worker.claim(db)
    assert first is not None
    assert worker.claim(db) is None
    with db.session() as session:
        session.get(Job, first["id"]).lease_until = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
    second = worker.claim(db)
    assert second["lease_token"] != first["lease_token"]
    assert batches(db)[0]["spent_worker_seconds"] >= 120
    with pytest.raises(WorkStopped):
        worker.execute(db, first)
    with db.session() as session:
        assert session.get(JobExecution, first["lease_token"]).state == "expired"
        assert session.get(ProcessingBatch, batch["id"]).state == "active"


def test_plan_api_preview_then_cancel(db, sources):
    with TestClient(create_app(db.settings)) as client:
        body = request(sources[0], max_jobs=1).model_dump()
        preview = client.post("/api/plans/preview", json=body).json()
        assert preview["deferred_units"] == 59
        batch = client.post(
            "/api/batches", json={"request": body, "expected_plan_sha256": preview["sha256"]}
        ).json()
        assert client.get("/api/catalog").json()["batches"][0]["id"] == batch["id"]
        result = client.post(f"/api/batches/{batch['id']}/control", json={"action": "cancel"})
        assert result.json()["state"] == "cancelled"


def test_preview_is_pinned_and_source_change_after_enqueue_stops_batch(db, sources, media):
    req = request(sources[0], max_jobs=1)
    plan = preview_plan(db, req)
    with db.session() as session:
        row = session.get(Source, sources[0]["id"])
        row.document = {**row.document, "metadata_sha256": "changed"}
    with pytest.raises(ValueError, match="changed since preview"):
        create_batch(db, req, plan["sha256"])
    batch = create_batch(db, req)
    with db.session() as session:
        row = session.get(Source, sources[0]["id"])
        row.document = {**row.document, "metadata_sha256": "changed-again"}
    assert worker.run_one(db)["state"] == "failed"
    assert batches(db)[0]["state"] == "paused"
    assert batches(db)[0]["id"] == batch["id"]
    assert not media
