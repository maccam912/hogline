import copy

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import func, select

from hogline.adapters.sources import import_youtube
from hogline.db import Database, FrameObservation, Job, JobExecution, ProcessingBatch, Source
from hogline.domain.catalog import Conflict, catalog
from hogline.orchestration import automatic, ocr_jobs, windows, worker
from hogline.orchestration.automatic import AutomaticRequest, create_automatic_batch, preview_automatic
from hogline.orchestration.plans import BatchControl, control_batch
from hogline.perception.ocr import OCRBudgetReached
from hogline.review.app import create_app


@pytest.fixture
def media(monkeypatch):
    monkeypatch.setattr(worker, "resolve_media", lambda source, **kw: {"uri": "fixture"})
    monkeypatch.setattr(windows, "resolve_media", lambda source, **kw: {"uri": "fixture"})

    def capture(_, seconds):
        return Image.new("RGB", (640, 360), "white"), {
            "pts": round(seconds * 1000),
            "time_base_num": 1,
            "time_base_den": 1000,
        }

    def decode(_, start, end, interval, **kw):
        for i in range(round((end - start) / interval)):
            yield capture(_, start + i * interval)

    monkeypatch.setattr(worker, "capture", capture)
    monkeypatch.setattr(windows, "decode_window", decode)

    class Reader:
        available = True

        def __init__(self, deadline):
            self.deadline = deadline

        def read(self, *args, **kwargs):
            return {"text": "", "engine_confidence": None, "tokens": []}

    monkeypatch.setattr(ocr_jobs, "TesseractOCR", Reader)
    return Reader


def campaign(sources, **kwargs):
    return AutomaticRequest(
        source_ids=[s["id"] for s in sources],
        end_seconds=60,
        scan_jobs=8,
        detail_jobs=8,
        ocr_frames=24,
        **kwargs,
    )


def batch(db, identifier):
    with db.session() as session:
        row = session.get(ProcessingBatch, identifier)
        return {"state": row.state, **copy.deepcopy(row.document)}


def drain(db):
    outcomes = []
    for _ in range(160):
        item = worker.run_one(db)
        if item is None:
            return outcomes
        assert item["state"] == "succeeded", item
        outcomes.append(item)
    pytest.fail("Campaign did not terminate")


def test_full_campaign_across_eight_sheets_survives_restart_and_preserves_lineage(db, media):
    sheets = [
        import_youtube(
            db,
            f"https://www.youtube.com/watch?v=Z{i:010d}",
            {"id": f"Z{i:010d}", "title": f"SHEET {i} | 03-17-26", "duration": 90},
        )
        for i in range(1, 9)
    ]
    request = campaign(sheets)
    preview = preview_automatic(db, request)
    assert preview["sha256"] == preview_automatic(db, request)["sha256"]
    result = create_automatic_batch(db, request, preview["sha256"])
    assert result["phase"] == "scan" and len(result["units"]) == 8
    for _ in range(8):
        assert worker.run_one(db)["state"] == "succeeded"
    # The last coarse job must not prematurely complete the campaign.
    assert batch(db, result["id"])["state"] == "active"
    resumed = Database(db.settings)
    remaining = drain(resumed)
    doc = batch(db, result["id"])
    assert doc["state"] == "completed" and doc["phase"] == "ocr"
    assert [p["phase"] for p in doc["phase_history"]] == ["detail", "ocr"]
    assert doc["phase_history"][0]["selected_quiet_audits"] >= 2
    assert doc["phase_history"][1]["selected_frames"] == 16
    assert len(remaining) == 24  # Eight detail windows, sixteen retained frames.
    assert 0 < doc["reserved_image_bytes"] < request.max_image_growth_mb * 1024**2
    with db.session() as session:
        runs = list(session.scalars(select(JobExecution)))
        assert doc["spent_worker_seconds"] == pytest.approx(sum(e.document["wall_seconds"] for e in runs))
        observations = [
            o.document
            for o in session.scalars(select(FrameObservation))
            if o.document.get("kind") == "broadcast_observation"
        ]
        assert len(observations) == 16 and {o["sheet"] for o in observations} == set(range(1, 9))
        assert all(o["acceptance"] == "proposed" and "&t=" in o["youtube_url"] for o in observations)
    assert not catalog(db)["games"]
    assert worker.run_one(db) is None


def test_coarse_admission_is_fair_and_deferred_counts_are_explicit(db, sources):
    r = AutomaticRequest(source_ids=[s["id"] for s in sources], end_seconds=3000, scan_jobs=2)
    preview = preview_automatic(db, r)
    assert [u["payload"]["request"]["source_id"] for u in preview["units"]] == r.source_ids
    assert preview["deferred_units"] == 8
    assert preview["total_requested_units"] == 10
    with db.session() as session:
        assert session.scalar(select(func.count()).select_from(Job)) == 0


def test_pause_resume_cancel_and_shared_budget_survive_phase_boundary(db, sources, media):
    result = create_automatic_batch(db, campaign(sources[:1]))
    control_batch(db, result["id"], BatchControl(action="pause"))
    assert worker.run_one(db) is None
    control_batch(db, result["id"], BatchControl(action="resume"))
    assert worker.run_one(db)["state"] == "succeeded"
    with db.session() as session:
        row = session.get(ProcessingBatch, result["id"])
        row.document = {**row.document, "spent_worker_seconds": 600}
    assert worker.run_one(db) is None
    assert batch(db, result["id"])["state"] == "paused"
    assert len(batch(db, result["id"])["units"]) == 1
    control_batch(db, result["id"], BatchControl(action="resume", max_worker_seconds=900))
    assert worker.run_one(db)["state"] == "succeeded"
    assert batch(db, result["id"])["spent_worker_seconds"] > 600
    control_batch(db, result["id"], BatchControl(action="cancel"))
    assert worker.run_one(db) is None
    assert batch(db, result["id"])["state"] == "cancelled"
    assert len(batch(db, result["id"])["phase_history"]) == 1


def test_preview_revalidation_and_changed_source_blocks_next_phase(db, sources, media):
    r = campaign(sources[:1])
    preview = preview_automatic(db, r)
    result = create_automatic_batch(db, r, preview["sha256"])
    with pytest.raises(Conflict):
        create_automatic_batch(db, r, preview["sha256"])
    assert worker.run_one(db)["state"] == "succeeded"
    with db.session() as session:
        src = session.get(Source, sources[0]["id"])
        src.document = {**src.document, "metadata_sha256": "changed"}
    assert worker.run_one(db) is None
    doc = batch(db, result["id"])
    assert doc["state"] == "paused" and "metadata changed" in doc["pause_reason"]


def test_empty_later_stages_finish_and_reused_scan_does_not_finish_early(db, sources, media, monkeypatch):
    r = campaign(sources[:1])
    monkeypatch.setattr(
        automatic,
        "adaptive_in_session",
        lambda *args: {
            "units": [],
            "sha256": "x",
            "eligible_sparse_samples": 0,
            "deferred_units": 0,
            "exclusions": {},
            "selected_quiet_audits": 0,
        },
    )
    first = create_automatic_batch(db, r)
    drain(db)
    assert batch(db, first["id"])["state"] == "completed"
    second = create_automatic_batch(db, r)
    assert second["state"] == "active" and second["reused_succeeded_units"] == 1
    assert worker.run_one(db) is None
    assert batch(db, second["id"])["state"] == "completed"


def test_ocr_budget_stop_keeps_prior_observations_and_resumes_without_duplicates(
    db, sources, media, monkeypatch
):
    request = AutomaticRequest(source_ids=[sources[0]["id"]], end_seconds=60, scan_jobs=1, detail_jobs=1)
    result = create_automatic_batch(db, request)
    assert worker.run_one(db)["state"] == "succeeded"
    assert worker.run_one(db)["state"] == "succeeded"
    assert worker.run_one(db)["state"] == "succeeded"  # First anchor OCR.
    with db.session() as session:
        row = session.get(ProcessingBatch, result["id"])
        row.document = {**row.document, "spent_worker_seconds": 599}
    with monkeypatch.context() as patch:
        patch.setattr(media, "read", lambda *a, **kw: (_ for _ in ()).throw(OCRBudgetReached("fixture")))
        stopped = worker.run_one(db)
        assert stopped["state"] == "queued"
    doc = batch(db, result["id"])
    assert doc["state"] == "paused"
    control_batch(db, result["id"], BatchControl(action="resume", max_worker_seconds=900))
    drain(db)
    doc = batch(db, result["id"])
    assert doc["state"] == "completed"
    with db.session() as session:
        assert session.get(Job, stopped["id"]).attempts == 1
        assert (
            session.scalar(
                select(func.count())
                .select_from(FrameObservation)
                .where(FrameObservation.document["kind"].as_string() == "broadcast_observation")
            )
            == 2
        )
        assert sum(e.document["wall_seconds"] for e in session.scalars(select(JobExecution))) > 0


def test_method_change_abstains_and_api_is_readonly_until_start(db, sources, media, monkeypatch):
    request = campaign(sources[:1])
    with TestClient(create_app(db.settings)) as client:
        preview = client.post("/api/automatic/preview", json=request.model_dump())
        assert preview.status_code == 200
        with db.session() as session:
            assert session.scalar(select(func.count()).select_from(ProcessingBatch)) == 0
        started = client.post(
            "/api/automatic/batches",
            json={"request": request.model_dump(), "expected_plan_sha256": preview.json()["sha256"]},
        )
        assert started.status_code == 200
    assert worker.run_one(db)["state"] == "succeeded"
    monkeypatch.setattr(automatic, "VERSION", "new-method")
    assert worker.run_one(db) is None
    assert "methods changed" in batch(db, started.json()["id"])["pause_reason"]


def test_stage_publication_rolls_back_all_jobs_on_interruption(db, sources, media, monkeypatch):
    result = create_automatic_batch(db, campaign(sources[:1]))
    assert worker.run_one(db)["state"] == "succeeded"
    with db.session() as session:
        before = session.scalar(select(func.count()).select_from(Job))
    original = automatic.enqueue_in_session
    calls = 0

    def interrupted(*args, **kwargs):
        nonlocal calls
        calls += 1
        job = original(*args, **kwargs)
        if calls == 2:
            raise RuntimeError("crash during phase publication")
        return job

    with monkeypatch.context() as patch:
        patch.setattr(automatic, "enqueue_in_session", interrupted)
        with pytest.raises(RuntimeError, match="phase publication"):
            automatic.advance_automatic(db)
    with db.session() as session:
        assert session.scalar(select(func.count()).select_from(Job)) == before
    assert batch(db, result["id"])["phase"] == "scan"
    assert batch(db, result["id"])["phase_history"] == []
    drain(db)
    assert batch(db, result["id"])["state"] == "completed"


def test_corrupt_ocr_evidence_fails_without_downloading_or_inventing_reading(db, sources, media, monkeypatch):
    result = create_automatic_batch(
        db, AutomaticRequest(source_ids=[sources[0]["id"]], end_seconds=60, scan_jobs=1, detail_jobs=1)
    )
    assert worker.run_one(db)["state"] == "succeeded"
    assert worker.run_one(db)["state"] == "succeeded"
    automatic.advance_automatic(db)
    doc = batch(db, result["id"])
    unit = next(u for u in doc["units"] if u["payload"].get("kind") == "broadcast_ocr")
    with db.session() as session:
        frame = session.get(automatic.Evidence, unit["payload"]["evidence_id"])
        path = db.settings.data_dir / frame.document["image"]["path"]
    original_bytes = path.read_bytes()
    path.write_bytes(b"corrupt")
    failed = worker.run_one(db)
    assert failed["state"] == "failed" and "corrupt evidence" in failed["error"]
    assert batch(db, result["id"])["state"] == "paused"
    path.write_bytes(original_bytes)
    worker.retry(db, failed["id"])
    control_batch(db, result["id"], BatchControl(action="resume"))
    drain(db)
    assert batch(db, result["id"])["state"] == "completed"
