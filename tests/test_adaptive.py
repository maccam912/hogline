import copy

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import func, select

from hogline.adapters.sources import digest, import_youtube
from hogline.config import profile
from hogline.db import Evidence, Job, ProcessingBatch, Scan, Source
from hogline.domain.catalog import Conflict, catalog
from hogline.domain.models import ScanRequest
from hogline.media.frames import observe
from hogline.orchestration import windows, worker
from hogline.orchestration.adaptive import AdaptiveRequest, create_adaptive_batch, preview_adaptive
from hogline.orchestration.plans import BatchControl, control_batch, payload_for
from hogline.reconstruction.activity import VERSION
from hogline.review.app import create_app


@pytest.fixture
def scanned_sheets(db):
    sources = []
    for sheet in range(1, 9):
        key = f"C{sheet:010d}"
        source = import_youtube(
            db,
            f"https://www.youtube.com/watch?v={key}",
            {
                "id": key,
                "title": f"SHEET {sheet} | 03-17-26",
                "duration": 100,
            },
        )
        sources.append(source)
        request = ScanRequest(source_id=source["id"], start_seconds=0, end_seconds=80, interval_seconds=20)
        payload = payload_for(source, request)
        with db.session() as session:
            job = Job(job_key=digest(payload), state="succeeded", document=payload)
            session.add(job)
            session.flush()
            samples = []
            for seconds, state in [
                (10, "unknown"),
                (30, "quiet_candidate"),
                (50, "activity_candidate"),
                (70, "quiet_candidate"),
            ]:
                image = Image.new("RGB", (640 if sheet == 2 else 960, 360 if sheet == 2 else 540), "white")
                doc, _ = observe(
                    image,
                    {"pts": seconds, "time_base_num": 1, "time_base_den": 1},
                    source,
                    db.settings.data_dir,
                )
                frame = Evidence(source_id=source["id"], evidence_key=doc["evidence_key"], document=doc)
                session.add(frame)
                session.flush()
                samples.append({"seconds": seconds, "state": state, "evidence_id": frame.id, "owned": True})
            session.add(
                Scan(
                    source_id=source["id"],
                    job_id=job.id,
                    document={
                        "request": request.model_dump(),
                        "version": VERSION,
                        "profile": profile(),
                        "samples": samples,
                    },
                )
            )
    return sources


def request(sources, **kwargs):
    return AdaptiveRequest(source_ids=[s["id"] for s in sources], **kwargs)


def test_preview_is_stable_bounded_fair_across_eight_sheets_and_keeps_quiet_audit(db, scanned_sheets):
    r = request(scanned_sheets, max_jobs=8)
    plan = preview_adaptive(db, r)
    assert plan["sha256"] == preview_adaptive(db, r)["sha256"]
    assert plan["eligible_sparse_samples"] == 32
    assert plan["selected_quiet_audits"] == 2
    assert len(plan["units"]) == 8
    assert {u["selection"]["source_id"] for u in plan["units"]} == set(r.source_ids)
    assert all(u["selection"]["reason"] in ("quiet_audit", "activity_transition") for u in plan["units"])
    assert all(
        u["selection"]["youtube_url"].startswith("https://www.youtube.com/watch?") for u in plan["units"]
    )
    with db.session() as session:
        assert session.scalar(select(func.count()).select_from(Job)) == 8  # Scans only.
        assert session.scalar(select(func.count()).select_from(ProcessingBatch)) == 0


def test_overlapping_candidates_are_not_queued_twice_and_range_is_honored(db, scanned_sheets):
    plan = preview_adaptive(
        db, request(scanned_sheets[:1], start_seconds=20, end_seconds=75, window_seconds=40, max_jobs=8)
    )
    assert len(plan["units"]) == 1
    r = plan["units"][0]["payload"]["request"]
    assert 20 <= r["start_seconds"] < r["end_seconds"] <= 75


def test_stale_sources_and_methods_are_excluded_and_preview_revalidated_atomically(db, scanned_sheets):
    r = request(scanned_sheets[:2])
    plan = preview_adaptive(db, r)
    with db.session() as session:
        source = session.get(Source, scanned_sheets[0]["id"])
        source.document = {**source.document, "metadata_sha256": "a" * 64}
    with pytest.raises(Conflict, match="Preview again"):
        create_adaptive_batch(db, r, plan["sha256"])
    updated = preview_adaptive(db, r)
    assert updated["exclusions"]["stale_source_scan"] == 1
    assert all(u["selection"]["source_id"] == scanned_sheets[1]["id"] for u in updated["units"])
    with db.session() as session:
        scan = session.scalar(select(Scan).where(Scan.source_id == scanned_sheets[1]["id"]))
        scan.document = {**scan.document, "version": "outdated-method"}
    assert preview_adaptive(db, r)["units"] == []


def test_unrelated_board_profile_changes_do_not_discard_valid_motion(db, scanned_sheets, monkeypatch):
    from hogline.orchestration import adaptive

    changed = copy.deepcopy(profile())
    changed["broadcast"] = {"changed_board_only": True}
    monkeypatch.setattr(adaptive, "profile", lambda: changed)
    assert preview_adaptive(db, request(scanned_sheets[:1]))["units"]
    changed["views"]["house_top"][0] += 0.01
    assert preview_adaptive(db, request(scanned_sheets[:1]))["units"] == []


def test_dense_batch_pauses_resumes_and_skips_current_completed_work(db, scanned_sheets, monkeypatch):
    monkeypatch.setattr(windows, "resolve_media", lambda source: {"uri": "fixture"})

    def decode(_, start, end, interval, **kwargs):
        for n in range(round((end - start) / interval)):
            yield (
                Image.new("RGB", (640, 360), "white"),
                {
                    "pts": round((start + n * interval) * 1000),
                    "time_base_num": 1,
                    "time_base_den": 1000,
                },
            )

    monkeypatch.setattr(windows, "decode_window", decode)
    r = request(scanned_sheets[:1], max_jobs=2)
    preview = preview_adaptive(db, r)
    batch = create_adaptive_batch(db, r, preview["sha256"])
    assert batch["kind"] == "adaptive_windows"
    assert batch["units"][0]["selection"]["reason"] == "quiet_audit"
    control_batch(db, batch["id"], BatchControl(action="pause"))
    assert worker.run_one(db) is None
    control_batch(db, batch["id"], BatchControl(action="resume"))
    first_job = worker.run_one(db)
    assert first_job["state"] == "succeeded"
    assert first_job["id"] == batch["units"][0]["job_id"]
    assert worker.run_one(db)["state"] == "succeeded"
    data = catalog(db)
    assert len(data["analysis_windows"]) == 2 and not data["games"]
    with db.session() as session:
        stored = session.get(ProcessingBatch, batch["id"])
        assert stored.state == "completed"
        assert 0 < stored.document["reserved_image_bytes"] <= r.max_image_growth_mb * 1024**2
        assert stored.document["spent_worker_seconds"] > 0
    next_plan = preview_adaptive(db, r)
    assert next_plan["exclusions"]["already_completed"] == 2
    assert len(next_plan["units"]) == 2


def test_api_preview_queues_nothing_and_duplicate_start_requires_new_preview(db, scanned_sheets):
    r = request(scanned_sheets[:1], max_jobs=1)
    with TestClient(create_app(db.settings)) as client:
        preview = client.post("/api/adaptive/preview", json=r.model_dump()).json()
        body = {"request": r.model_dump(), "expected_plan_sha256": preview["sha256"]}
        first = client.post("/api/adaptive/batches", json=body)
        assert first.status_code == 200
        assert client.post("/api/adaptive/batches", json=body).status_code == 409
