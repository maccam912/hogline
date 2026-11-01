import copy
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import event, insert, select
from sqlalchemy.exc import OperationalError

from hogline.db import Evidence, FrameObservation, Job, rows_by_ids, temporary_database_lock
from hogline.domain.models import ScanRequest
from hogline.domain.provenance import resolve_observation_manifests
from hogline.orchestration import worker
from hogline.orchestration.windows import WindowRequest, enqueue_window
from hogline.perception import pipeline
from tests.test_automatic import media  # noqa: F401


def test_manifest_resolution_exceeds_database_bind_limit_without_losing_claims(db, sources):
    source = sources[0]
    values = [
        {
            "id": f"frame-{i:05}",
            "source_id": source["id"],
            "evidence_key": f"frame-{i}",
            "document": {"source_metadata_sha256": source["metadata_sha256"]},
        }
        for i in range(1203)
    ]
    with db.session() as session:
        session.execute(insert(Evidence), values)
    observations = [{"id": f"o-{r['id']}", "evidence_id": r["id"], "source_id": source["id"]} for r in values]
    with db.session() as session:
        connection = session.connection().connection.driver_connection
        previous = connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 800)
        try:
            resolved = resolve_observation_manifests(session, observations)
            assert [o["id"] for o in resolved] == [o["id"] for o in observations]
            assert all(o["source_manifest_resolution"]["status"] == "resolved" for o in resolved)
            assert list(rows_by_ids(session, Evidence, Evidence.id, [])) == []
        finally:
            connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, previous)


def test_write_acquisition_waits_for_temporary_lock_without_replaying_body(db):
    attempts, bodies = [], []

    @event.listens_for(db.engine, "connect")
    def quick_busy_timeout(connection, _):
        connection.execute("PRAGMA busy_timeout=10")

    @event.listens_for(db.engine, "before_cursor_execute")
    def count_begin(connection, cursor, statement, parameters, context, many):
        if statement == "BEGIN IMMEDIATE":
            attempts.append(statement)

    db.engine.dispose()

    def waiting_writer():
        with db.snapshot_session(write=True) as session:
            bodies.append(1)
            session.add(Job(id="lock-test", job_key="lock-test", document={}))

    with ThreadPoolExecutor(max_workers=1) as pool:
        with db.snapshot_session(write=True):
            pending = pool.submit(waiting_writer)
            time.sleep(0.25)
            assert not pending.done()
        pending.result(timeout=5)
    assert len(attempts) > 2 and bodies == [1]
    with db.session() as session:
        assert session.get(Job, "lock-test") is not None


def test_database_lock_classification_does_not_retry_invalid_queries():
    assert temporary_database_lock(
        OperationalError("BEGIN", {}, sqlite3.OperationalError("database is locked"))
    )
    assert not temporary_database_lock(
        OperationalError("SELECT", {}, sqlite3.OperationalError("too many SQL variables"))
    )
    assert not temporary_database_lock(ValueError("database is locked"))


@pytest.mark.usefixtures("media")
def test_temporary_database_failure_retries_same_job_and_preserves_failure_history(db, sources, monkeypatch):
    queued = worker.enqueue(db, ScanRequest(source_id=sources[0]["id"], end_seconds=60, interval_seconds=60))
    original, calls = worker.execute, []

    def locked_once(database, job):
        calls.append(job["id"])
        if len(calls) == 1:
            raise OperationalError("BEGIN IMMEDIATE", {}, sqlite3.OperationalError("database is locked"))
        return original(database, job)

    monkeypatch.setattr(worker, "execute", locked_once)
    assert worker.run_one(db)["state"] == "queued"
    with db.session() as session:
        row = session.get(Job, queued["id"])
        assert row.document["retry_pending"] and row.document["failure_history"][0]["automatic_retry"]
        row.document = {**row.document, "retry_after": None}
    assert worker.run_one(db)["state"] == "succeeded"
    assert calls == [queued["id"], queued["id"]]
    with db.session() as session:
        assert len(session.get(Job, queued["id"]).document["failure_history"]) == 1


@pytest.mark.parametrize("kind", ["scan", "window"])
@pytest.mark.usefixtures("media")
def test_frame_analysis_allows_an_independent_writer_before_atomic_publication(
    db, sources, monkeypatch, kind
):
    original, writes = pipeline.analyze_frame, []

    def check_writer(*args, **kwargs):
        with sqlite3.connect(db.engine.url.database, timeout=0.05) as other:
            other.execute("BEGIN IMMEDIATE")
            writes.append(1)
            other.rollback()
        return original(*args, **kwargs)

    monkeypatch.setattr(pipeline, "analyze_frame", check_writer)
    if kind == "scan":
        worker.enqueue(
            db,
            ScanRequest(
                source_id=sources[0]["id"], end_seconds=120, interval_seconds=60, acquisition_tier="evidence"
            ),
        )
    else:
        enqueue_window(
            db,
            WindowRequest(
                source_id=sources[0]["id"],
                start_seconds=0,
                end_seconds=4,
                interval_seconds=1,
                analyze_actors=False,
            ),
        )
    assert worker.run_one(db)["state"] == "succeeded"
    assert writes
    with db.session() as session:
        assert len(list(session.scalars(select(FrameObservation)))) >= 2


@pytest.mark.parametrize("failure_phase", ["detail", "reconstruct"])
@pytest.mark.usefixtures("media")
def test_operator_restart_preserves_failed_history_and_retries_after_retention_cleanup(
    db, monkeypatch, failure_phase
):
    from hogline.adapters.sources import digest, import_youtube
    from hogline.db import ProcessingBatch, Scan
    from hogline.orchestration import recording, windows
    from hogline.orchestration.recording import (
        RecordingAnalysisRequest,
        restart_failed_recording,
        start_recording_analysis,
    )

    source = import_youtube(
        db,
        "https://www.youtube.com/watch?v=CCCCCCCCCCC",
        {
            "id": "CCCCCCCCCCC",
            "title": "SHEET 3 | 03-17-26",
            "duration": 180,
        },
    )
    batch = start_recording_analysis(db, source["id"], RecordingAnalysisRequest(detail_windows=1))
    original_window, original_timeline = windows.execute_window, recording.source_end_timeline

    def broken_window(*args, **kwargs):
        raise ValueError("Invalid frame checksum")

    def broken_reconstruction(*args, **kwargs):
        raise OperationalError("SELECT", {}, sqlite3.OperationalError("too many SQL variables"))

    if failure_phase == "detail":
        monkeypatch.setattr(windows, "execute_window", broken_window)
    else:
        monkeypatch.setattr(recording, "source_end_timeline", broken_reconstruction)
    for _ in range(60):
        if worker.run_one(db) is None:
            break
    else:
        pytest.fail("Failed analysis did not release the queue")
    with db.session() as session:
        failed = session.get(ProcessingBatch, batch["id"])
        assert failed.state == "failed" and failed.document["image_cleanup"]
        failed_jobs = [session.get(Job, u["job_id"]) for u in failed.document["units"]]
        failed_ids = [j.id for j in failed_jobs if j.state == "failed"]
        old_scan_ids = set(session.scalars(select(Scan.id)))
    if failure_phase == "detail":
        # A previous, partially read dense window left an unprocessed descriptor after image eviction.
        with db.session() as session:
            frame = session.scalar(select(Evidence))
            stale = copy.deepcopy(frame.document)
            stale.update(
                {"id": "evicted-old-anchor", "evidence_key": "evicted-old-anchor", "source_seconds": 19.25}
            )
            stale["image"]["path"] = "evidence/evicted-old-anchor.png"
            session.add(
                Evidence(
                    id=stale["id"], evidence_key=stale["evidence_key"], source_id=source["id"], document=stale
                )
            )
    monkeypatch.setattr(windows, "execute_window", original_window)
    monkeypatch.setattr(recording, "source_end_timeline", original_timeline)
    captures, original_capture = [], worker.capture

    def capture_again(*args, **kwargs):
        captures.append(1)
        if failure_phase == "reconstruct":
            pytest.fail("Reconstruction retry must use saved measurements without acquiring images")
        return original_capture(*args, **kwargs)

    monkeypatch.setattr(worker, "capture", capture_again)
    restarted = restart_failed_recording(db, batch["id"])
    assert restarted["state"] == "active"
    for _ in range(60):
        result = worker.run_one(db)
        if result is None:
            break
        assert result["state"] == "succeeded", result
    else:
        pytest.fail("Restarted recording did not terminate")
    with db.session() as session:
        row = session.get(ProcessingBatch, batch["id"])
        assert row.state == "completed" and row.document["image_cleanup"]
        assert digest(row.document["analysis_result"]) == row.document["result_sha256"]
        assert row.document["restart_history"][-1]["failed_job_ids"] == failed_ids
        for identifier in failed_ids:
            assert session.get(Job, identifier).document["failure_history"]
        assert old_scan_ids <= set(session.scalars(select(Scan.id)))
        if failure_phase == "detail":
            assert captures and len(set(session.scalars(select(Scan.id)))) > len(old_scan_ids)
            assert all(
                session.get(Job, u["job_id"]).job_key == digest(u["payload"]) for u in row.document["units"]
            )
            assert all(identifier not in row.document["unit_outcomes"] for identifier in failed_ids)
        else:
            assert not captures
    with pytest.raises(ValueError, match="Only a failed"):
        restart_failed_recording(db, batch["id"])


def test_operator_retry_keeps_already_open_workspace_ahead_of_restarted_recording(db, sources):
    from hogline.db import ProcessingBatch
    from hogline.orchestration.recording import (
        RecordingAnalysisRequest,
        restart_failed_recording,
        start_recording_analysis,
    )

    first = start_recording_analysis(db, sources[0]["id"], RecordingAnalysisRequest())
    peer = start_recording_analysis(db, sources[1]["id"], RecordingAnalysisRequest())
    with db.session() as session:
        failed = session.get(ProcessingBatch, first["id"])
        failed.state = "failed"
        for unit in failed.document["units"]:
            session.get(Job, unit["job_id"]).state = "failed"
        opened = session.get(ProcessingBatch, peer["id"])
        opened.document = {**opened.document, "spent_worker_seconds": 10}
    restart_failed_recording(db, first["id"])
    assert worker.claim(db)["batch_id"] == peer["id"]
    with db.session() as session:
        restarted = session.get(ProcessingBatch, first["id"])
        assert restarted.document["restart_history"][-1]["created_at"] == first["created_at"]
