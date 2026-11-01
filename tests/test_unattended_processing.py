import copy
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from hogline.adapters.sources import digest, import_youtube
from hogline.db import Database, Evidence, Job, JobExecution, ProcessingBatch, now
from hogline.orchestration import windows, worker
from hogline.orchestration.automatic import advance_automatic
from hogline.orchestration.plans import BatchControl, control_batch, finish_execution
from hogline.orchestration.pool import WorkerPool, stalled_workers
from hogline.orchestration.recording import RecordingAnalysisRequest, start_recording_analysis
from tests.test_automatic import media  # noqa: F401


def recordings(db):
    sources = [
        import_youtube(
            db,
            f"https://www.youtube.com/watch?v={i:011d}",
            {
                "id": f"{i:011d}",
                "title": f"SHEET {i} | 03-17-26",
                "duration": 180,
            },
        )
        for i in (1, 2)
    ]
    return [
        start_recording_analysis(db, s["id"], RecordingAnalysisRequest(detail_windows=1)) for s in sources
    ]


def drain(db):
    for _ in range(100):
        if worker.run_one(db) is None:
            return
    pytest.fail("Unattended queue failed to terminate")


@pytest.mark.usefixtures("media")
def test_timeout_replacement_preserves_payloads_and_completes_both_recordings_across_restart(db, monkeypatch):
    batches = recordings(db)
    original = windows.execute_window
    timeouts = []

    def timeout_once(database, job):
        if not timeouts:
            timeouts.append(copy.deepcopy(job))
            raise ValueError("Detailed window worker-time budget reached")
        return original(database, job)

    monkeypatch.setattr(windows, "execute_window", timeout_once)
    drain(Database(db.settings))
    assert len(timeouts) == 1
    with db.session() as session:
        for b in batches:
            row = session.get(ProcessingBatch, b["id"])
            assert row.state == "completed" and row.document["image_cleanup"]
        row = session.get(ProcessingBatch, batches[0]["id"])
        recovery = row.document["recovery_history"][0]
        failed = session.get(Job, recovery["failed_job_id"])
        retried = session.get(Job, recovery["replacement_job_id"])
        assert failed.state == "failed" and retried.state == "succeeded"
        assert failed.document["request"] == timeouts[0]["request"]
        assert retried.document["request"]["max_worker_seconds"] == 240
        assert retried.job_key == digest(
            next(u["payload"] for u in row.document["units"] if u["job_id"] == retried.id)
        )
        assert failed.id not in row.document["unit_outcomes"]
        assert any(retried.id in p["job_ids"] for p in row.document["phase_history"])


@pytest.mark.parametrize("error", ["Detailed window worker-time budget reached", "Invalid frame checksum"])
@pytest.mark.usefixtures("media")
def test_repeat_timeout_or_invalid_evidence_releases_images_and_next_recording_finishes(
    db, monkeypatch, error
):
    batches = recordings(db)
    original = windows.execute_window
    attempts = []

    def broken(database, job):
        if job["batch_id"] == batches[0]["id"]:
            attempts.append(job["id"])
            raise ValueError(error)
        return original(database, job)

    monkeypatch.setattr(windows, "execute_window", broken)
    drain(db)
    with db.session() as session:
        bad = session.get(ProcessingBatch, batches[0]["id"])
        good = session.get(ProcessingBatch, batches[1]["id"])
        assert bad.state == "failed" and bad.document["analysis_outcome"] == "needs_attention"
        assert bad.document["image_cleanup"] and "analysis_result" not in bad.document
        assert good.state == "completed" and good.document["image_cleanup"]
        assert all(
            session.get(Job, u["job_id"]).state not in ("queued", "running") for u in bad.document["units"]
        )
        assert session.scalar(select(Evidence.id))  # Descriptors survive cleanup.
    assert len(attempts) == (2 if "budget" in error else 1)
    with pytest.raises(ValueError, match="new analysis"):
        control_batch(db, batches[0]["id"], BatchControl(action="resume", retry_failed=True))


@pytest.mark.usefixtures("media")
def test_batch_budget_failure_moves_on_without_raising_limits(db):
    batches = recordings(db)
    with db.session() as session:
        row = session.get(ProcessingBatch, batches[0]["id"])
        limits = copy.deepcopy(row.document["limits"])
        row.document = {**row.document, "spent_worker_seconds": limits["max_worker_seconds"]}
    assert worker.run_one(db)["state"] == "queued"  # Execution cooperatively stopped by its batch budget.
    drain(db)
    with db.session() as session:
        row = session.get(ProcessingBatch, batches[0]["id"])
        assert row.state == "failed" and row.document["limits"] == limits
        assert session.get(ProcessingBatch, batches[1]["id"]).state == "completed"


def test_operator_pause_still_holds_recording_slot(db):
    batches = recordings(db)
    control_batch(db, batches[0]["id"], BatchControl(action="pause"))
    assert worker.run_one(db) is None
    with db.session() as session:
        assert session.get(ProcessingBatch, batches[0]["id"]).state == "paused"


def test_failed_recording_waits_for_sibling_leases_before_cleanup_and_next_admission(db, sources):
    first = start_recording_analysis(db, sources[0]["id"], RecordingAnalysisRequest())
    second = start_recording_analysis(db, sources[1]["id"], RecordingAnalysisRequest())
    claims = [worker.claim(db) for _ in range(4)]
    with db.session() as session:
        job = session.get(Job, claims[0]["id"])
        job.state = "failed"
        job.document = {**job.document, "error": "Invalid frame checksum"}
        finish_execution(session, claims[0]["lease_token"], "failed", 1)
    advance_automatic(db)
    with db.session() as session:
        batch = session.get(ProcessingBatch, first["id"])
        assert batch.state == "paused" and not batch.document.get("image_cleanup")
    assert worker.claim(db) is None
    with db.session() as session:
        for claim in claims[1:]:
            job = session.get(Job, claim["id"])
            job.state = "queued"
            finish_execution(session, claim["lease_token"], "stopped", 1)
    # Queue recovery and cleanup happen before another source can acquire frames.
    assert worker.claim(db)["batch_id"] == second["id"]
    with db.session() as session:
        batch = session.get(ProcessingBatch, first["id"])
        assert batch.state == "failed" and batch.document["image_cleanup"]


@pytest.mark.usefixtures("media")
def test_recording_timeout_replacement_cap_survives_restart(db, monkeypatch):
    batches = recordings(db)
    original = windows.execute_window

    def timeout(database, job):
        if job["batch_id"] == batches[0]["id"]:
            raise ValueError("Detailed window worker-time budget reached")
        return original(database, job)

    monkeypatch.setattr(windows, "execute_window", timeout)
    for _ in range(30):
        outcome = worker.run_one(db)
        if outcome and outcome["state"] == "failed":
            break
    else:
        pytest.fail("Expected the injected timeout")
    with db.session() as session:
        row = session.get(ProcessingBatch, batches[0]["id"])
        row.document = {
            **row.document,
            "recovery_history": [{"action": "retry_timeout", "failed_job_id": str(i)} for i in range(3)],
        }
    drain(Database(db.settings))
    with db.session() as session:
        row = session.get(ProcessingBatch, batches[0]["id"])
        assert row.state == "failed" and len(row.document["recovery_history"]) == 4
        assert row.document["recovery_history"][-1]["action"] == "continue_queue"
        assert session.get(ProcessingBatch, batches[1]["id"]).state == "completed"


def test_watchdog_distinguishes_healthy_heartbeats_from_stalls_and_hard_deadline(db):
    recordings(db)
    job = worker.claim(db)
    current = datetime.now(UTC)
    assert stalled_workers(db, current) == set()
    with db.session() as session:
        row = session.get(Job, job["id"])
        execution = session.get(JobExecution, job["lease_token"])
        pid = execution.document["worker_pid"]
        row.lease_until = (current - timedelta(seconds=1)).isoformat()
    assert stalled_workers(db, current) == {pid}
    with db.session() as session:
        row = session.get(Job, job["id"])
        row.lease_until = (current + timedelta(seconds=120)).isoformat()
        execution = session.get(JobExecution, job["lease_token"])
        execution.document = {
            **execution.document,
            "started_at": (
                current - timedelta(seconds=job["request"]["max_worker_seconds"] + 121)
            ).isoformat(),
        }
    assert stalled_workers(db, current) == {pid}


def test_dead_process_is_automatically_replaced_and_its_lease_fenced(db):
    pool = WorkerPool(replace(db.settings, workers=1))
    try:
        pool.start()
        first = pool.processes[0]
        # A synthetic leased task avoids external media in the spawned process.
        with db.session() as session:
            task = Job(
                id="dead-task",
                job_key="dead-task",
                state="running",
                attempts=1,
                lease_until=(datetime.now(UTC) + timedelta(seconds=120)).isoformat(),
                document={"lease_token": "dead-token", "request": {"max_worker_seconds": 60}},
            )
            session.add(task)
            session.add(
                JobExecution(
                    id="dead-token",
                    job_id=task.id,
                    state="running",
                    document={"started_at": now(), "worker_pid": first.pid},
                )
            )
        first.kill()
        first.join(timeout=3)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and pool.processes[0].pid == first.pid:
            time.sleep(0.1)
        assert pool.processes[0].pid != first.pid
        assert pool.status()["alive_workers"] == 1 and pool.status()["worker_restarts"] >= 1
        # Restart reason and execution history survive process replacement.
        with db.session() as session:
            execution = session.get(JobExecution, "dead-token")
            assert execution.document["worker_restarted_at"]
            assert execution.document["restart_reason"] == "exited"
    finally:
        pool.close()
