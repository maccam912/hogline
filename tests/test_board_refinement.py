import copy

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import func, select

from hogline.config import profile
from hogline.db import Evidence, FrameObservation, Job, ProcessingBatch
from hogline.domain.catalog import Conflict
from hogline.media.frames import observe
from hogline.orchestration import ocr_jobs, windows, worker
from hogline.orchestration.board_refinement import (
    RefinementRequest,
    create_refinement_batch,
    preview_refinement,
    subtract,
)
from hogline.perception.broadcast import broadcast_catalog, broadcast_context
from hogline.review.app import create_app


def frame(db, source, seconds):
    doc, _ = observe(
        Image.new("RGB", (640, 360), "white"),
        {"pts": round(seconds * 1000), "time_base_num": 1, "time_base_den": 1000},
        source,
        db.settings.data_dir,
    )
    with db.session() as session:
        row = Evidence(source_id=source["id"], evidence_key=doc["evidence_key"], document=doc)
        session.add(row)
        session.flush()
        return row.id


@pytest.fixture
def change(db, sources):
    with db.session() as session:
        version = broadcast_context(session, db.settings.data_dir, profile())["version"]
    for seconds in (0, 5, 10, 110, 115, 120):
        identifier = frame(db, sources[0], seconds)
        doc = {
            "id": f"ocr-{seconds}",
            "evidence_id": identifier,
            "model_version": version,
            "source_id": sources[0]["id"],
            "source_metadata_sha256": sources[0]["metadata_sha256"],
            "source_seconds": seconds,
            "clock": {"candidates": []},
            "youtube_url": f"https://www.youtube.com/watch?v=AAAAAAAAAAA&t={seconds}s",
            "scoreboard": {
                "status": "observed",
                "cards": [] if seconds < 100 else [{"color": "red", "end_number": 1, "cumulative_points": 2}],
                "unreadable_slots": [],
                "conflicting_end_numbers": [],
            },
        }
        with db.session() as session:
            session.add(
                FrameObservation(id=doc["id"], evidence_id=identifier, model_version=version, document=doc)
            )
    return broadcast_catalog(db)["board_timeline"]["changes"][0]


@pytest.fixture
def media(monkeypatch):
    monkeypatch.setattr(windows, "resolve_media", lambda source: {"uri": "fixture"})

    def decode(_, start, end, interval, **kw):
        for i in range(round((end - start) / interval)):
            yield (
                Image.new("RGB", (640, 360), "white"),
                {"pts": round((start + i * interval) * 1000), "time_base_num": 1, "time_base_den": 1000},
            )

    monkeypatch.setattr(windows, "decode_window", decode)

    class Reader:
        available = True

        def __init__(self, deadline):
            self.deadline = deadline

        def read(self, *args, **kwargs):
            return {"text": "", "engine_confidence": None, "tokens": []}

    monkeypatch.setattr(ocr_jobs, "TesseractOCR", Reader)


def test_preview_selects_largest_gaps_without_fetching_media_or_enqueuing(db, change):
    request = RefinementRequest(max_rounds=1, change_id=change["id"], max_windows=3)
    result = preview_refinement(db, request)
    assert result["sha256"] == preview_refinement(db, request)["sha256"]
    spans = [
        (u["payload"]["request"]["start_seconds"], u["payload"]["request"]["end_seconds"])
        for u in result["units"]
    ]
    assert spans == [(55, 65), (27.5, 37.5), (82.5, 92.5)]
    assert result["estimated_new_sample_requests"] == 60
    assert result["board_change"]["before"]["source_seconds"] == 10
    with db.session() as session:
        assert session.scalar(select(func.count()).select_from(Job)) == 0
    assert subtract([(0, 10), (20, 30)], 5, 25) == [(0, 5), (25, 30)]


def test_refinement_only_reads_selected_anchors_under_one_batch_budget(db, sources, change, media):
    unrelated = frame(db, sources[0], 90)
    request = RefinementRequest(max_rounds=1, change_id=change["id"], max_windows=1)
    result = create_refinement_batch(db, request)
    assert result["phase"] == "detail" and len(result["units"]) == 1
    for _ in range(4):
        assert worker.run_one(db)["state"] == "succeeded"
    assert worker.run_one(db) is None
    with db.session() as session:
        row = session.get(ProcessingBatch, result["id"])
        assert row.state == "completed" and row.document["phase"] == "ocr"
        assert row.document["spent_worker_seconds"] > 0
        assert 0 < row.document["reserved_image_bytes"] < request.max_image_growth_mb * 1024**2
        assert (
            session.scalar(
                select(func.count())
                .select_from(FrameObservation)
                .where(FrameObservation.evidence_id == unrelated)
            )
            == 0
        )
        assert row.document["phase_history"][-1]["selected_frames"] == 3
        assert all(u["payload"].get("kind") != "scan" for u in row.document["units"])


def test_completed_windows_with_unread_anchors_are_reused_then_new_gaps_selected(db, sources, change, media):
    windows.enqueue_window(
        db, windows.WindowRequest(source_id=sources[0]["id"], start_seconds=55, end_seconds=65)
    )
    assert worker.run_one(db)["state"] == "succeeded"
    request = RefinementRequest(max_rounds=1, change_id=change["id"], max_windows=2)
    result = preview_refinement(db, request)
    assert result["reused_succeeded_units"] == 1
    assert result["units"][0]["selection"]["reason"] == "read_pending_board_anchors"
    next_request = result["units"][1]["payload"]["request"]
    assert next_request["start_seconds"] == 27.5 and next_request["end_seconds"] == 37.5
    queued = create_refinement_batch(db, request, result["sha256"])
    assert queued["state"] == "active"
    with pytest.raises(Conflict, match="matching work changed"):
        create_refinement_batch(db, request, result["sha256"])


def test_changed_readings_invalidate_preview_and_existing_work_is_not_stolen(db, change):
    request = RefinementRequest(max_rounds=1, change_id=change["id"], max_windows=1)
    preview = preview_refinement(db, request)
    batch = create_refinement_batch(db, request, preview["sha256"])
    other = preview_refinement(db, request)
    assert other["units"][0]["payload"]["request"]["start_seconds"] == 27.5
    assert other["excluded_intervals"][0]["job_id"] == batch["units"][0]["job_id"]
    with db.session() as session:
        for seconds in (110, 115, 120):
            row = session.get(FrameObservation, f"ocr-{seconds}")
            doc = copy.deepcopy(row.document)
            doc["scoreboard"]["cards"][0]["cumulative_points"] = 3
            row.document = doc
    with pytest.raises(Conflict, match="bracket changed"):
        create_refinement_batch(db, request, other["sha256"])


def test_api_requires_current_preview_and_reports_a_too_narrow_gap(db, change):
    request = RefinementRequest(max_rounds=1, change_id=change["id"], max_windows=1)
    with TestClient(create_app(db.settings)) as client:
        preview = client.post("/api/board-refinement/preview", json=request.model_dump())
        assert preview.status_code == 200
        response = client.post(
            "/api/board-refinement/batches",
            json={"request": request.model_dump(), "expected_plan_sha256": "0" * 64},
        )
        assert response.status_code == 409
    # Shift the entire second epoch to leave a two-second bracket.
    with db.session() as session:
        for seconds in (110, 115, 120):
            row = session.get(FrameObservation, f"ocr-{seconds}")
            row.document = {**row.document, "source_seconds": seconds - 98}
    new_change = broadcast_catalog(db)["board_timeline"]["changes"][0]
    narrow = RefinementRequest(max_rounds=1, change_id=new_change["id"])
    assert preview_refinement(db, narrow)["units"] == []
    with pytest.raises(ValueError, match="No new windows fit"):
        create_refinement_batch(db, narrow)


def drain(db):
    results = []
    for _ in range(100):
        result = worker.run_one(db)
        if result is None:
            return results
        assert result["state"] == "succeeded", result
        results.append(result)
    pytest.fail("Refinement did not honor its round bound")


def test_multiple_rounds_survive_restart_and_stop_unresolved_at_round_limit(db, change, media):
    from hogline.db import Database

    request = RefinementRequest(
        change_id=change["id"], max_windows=1, max_rounds=2, target_bracket_seconds=10
    )
    result = create_refinement_batch(db, request)
    for _ in range(4):
        assert worker.run_one(db)["state"] == "succeeded"
    with db.session() as session:
        row = session.get(ProcessingBatch, result["id"])
        assert row.state == "active" and row.document["phase"] == "ocr"
        first_cost = row.document["spent_worker_seconds"]
    restarted = Database(db.settings)
    assert len(drain(restarted)) == 4
    with db.session() as session:
        row = session.get(ProcessingBatch, result["id"])
        doc = row.document
        assert row.state == "completed" and doc["refinement_round"] == 2
        assert doc["refinement_outcome"]["status"] == "round_limit"
        assert doc["refinement_outcome"]["widest_bracket_seconds"] == 100
        assert doc["spent_worker_seconds"] > first_cost > 0
        assert [h["round"] for h in doc["phase_history"]] == [1, 1, 2, 2]
        assert len(doc["refinement_round_outcomes"]) == 2
        requests = [u["payload"]["request"] for u in doc["units"] if u["payload"]["kind"] == "dense_window"]
        assert [(r["start_seconds"], r["end_seconds"]) for r in requests] == [(55, 65), (27.5, 37.5)]
        assert doc["board_change"] == change


def test_target_reached_ends_rounds_early_without_accepting_a_score(db, change, media, monkeypatch):
    original = ocr_jobs.analyze_broadcast_frame

    def readable(*args, **kwargs):
        result = original(*args, **kwargs)
        session = args[0]
        result = {
            **result,
            "scoreboard": {
                "status": "observed",
                "cards": []
                if result["source_seconds"] < 60
                else [{"color": "red", "end_number": 1, "cumulative_points": 2}],
                "unreadable_slots": [],
                "conflicting_end_numbers": [],
            },
        }
        session.get(FrameObservation, result["id"]).document = result
        return result

    monkeypatch.setattr(ocr_jobs, "analyze_broadcast_frame", readable)
    result = create_refinement_batch(
        db, RefinementRequest(change_id=change["id"], max_windows=1, max_rounds=4, target_bracket_seconds=10)
    )
    assert len(drain(db)) == 4
    with db.session() as session:
        doc = session.get(ProcessingBatch, result["id"]).document
        assert doc["refinement_round"] == 1
        assert doc["refinement_outcome"]["status"] == "target_reached"
        assert doc["refinement_outcome"]["widest_bracket_seconds"] == 5
    event = broadcast_catalog(db)["board_timeline"]["changes"][0]
    assert event["before"]["source_seconds"] == 55 and event["after"]["source_seconds"] == 60
    assert event["acceptance"] == "proposed" and event["played_end_time_seconds"] is None


def test_budget_between_rounds_pauses_without_enqueuing_and_resume_keeps_costs(db, change, media):
    from hogline.orchestration.plans import BatchControl, control_batch

    result = create_refinement_batch(
        db, RefinementRequest(change_id=change["id"], max_windows=1, max_rounds=2, max_worker_seconds=300)
    )
    for _ in range(4):
        assert worker.run_one(db)["state"] == "succeeded"
    with db.session() as session:
        row = session.get(ProcessingBatch, result["id"])
        row.document = {**row.document, "spent_worker_seconds": 300}
    assert worker.run_one(db) is None
    with db.session() as session:
        row = session.get(ProcessingBatch, result["id"])
        assert row.state == "paused" and row.document["refinement_round"] == 1
        assert len(row.document["units"]) == 4
    control_batch(db, result["id"], BatchControl(action="resume", max_worker_seconds=600))
    assert len(drain(db)) == 4
    with db.session() as session:
        doc = session.get(ProcessingBatch, result["id"]).document
        assert doc["spent_worker_seconds"] > 300
        assert [h["round"] for h in doc["refinement_round_outcomes"]] == [1, 2]


def test_next_round_never_expands_original_color_source_or_interval(db, change, monkeypatch):
    from hogline.orchestration import board_refinement

    other = []
    for field, value in (("color", "yellow"), ("source_id", "another"), ("model_version", "another")):
        other.append({**change, field: value})
    other.append({**change, "before": {**change["before"], "source_seconds": 0}})
    monkeypatch.setattr(
        board_refinement, "broadcast_catalog_in_session", lambda *args: {"board_timeline": {"changes": other}}
    )
    with db.session() as session:
        plan, outcome = board_refinement.next_refinement(
            session,
            db.settings.data_dir,
            {
                "board_change": change,
                "refinement_round": 1,
                "refinement_request": RefinementRequest(change_id=change["id"]).model_dump(),
            },
        )
    assert plan is None and outcome["status"] == "no_current_matching_bracket"
    assert outcome["remaining_brackets"] == []


def test_failed_round_publication_leaves_no_orphan_jobs_or_round_increment(db, change, media, monkeypatch):
    from hogline.orchestration import automatic
    from hogline.orchestration.plans import BatchControl, control_batch

    result = create_refinement_batch(
        db, RefinementRequest(change_id=change["id"], max_windows=1, max_rounds=2, target_bracket_seconds=10)
    )
    for _ in range(4):
        assert worker.run_one(db)["state"] == "succeeded"
    original = automatic.enqueue_in_session

    def failure(*args, **kwargs):
        original(*args, **kwargs)
        raise ValueError("injected publication failure")

    with monkeypatch.context() as patch:
        patch.setattr(automatic, "enqueue_in_session", failure)
        assert worker.run_one(db) is None
    with db.session() as session:
        row = session.get(ProcessingBatch, result["id"])
        assert row.state == "paused" and row.document["refinement_round"] == 1
        assert row.document["phase"] == "ocr"
        assert len(row.document["units"]) == 4
        assert session.scalar(select(func.count()).select_from(Job)) == 4
    control_batch(db, result["id"], BatchControl(action="resume"))
    assert len(drain(db)) == 4
