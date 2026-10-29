import subprocess
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from hogline.db import AnalysisWindow, Database, Evidence, Job, JobExecution, ProcessingBatch, Scan
from hogline.media import frames
from hogline.media import windows as media_windows
from hogline.media.errors import TransientMediaError, remote_options
from hogline.orchestration import windows as window_jobs
from hogline.orchestration import worker
from hogline.orchestration.plans import BatchControl, PlanRequest, control_batch, create_batch
from tests.test_dense_analysis import fake_window  # noqa: F401
from tests.test_worker import fake_capture


@pytest.fixture(autouse=True)
def fake_media(monkeypatch):
    monkeypatch.setattr(
        worker, "resolve_media", lambda _, **_kwargs: {"uri": "fixture", "format_id": "fixture"}
    )
    monkeypatch.setattr(worker, "capture", fake_capture)


def batch(db, source):
    return create_batch(
        db,
        PlanRequest(
            source_ids=[source["id"]],
            end_seconds=120,
            chunk_seconds=60,
            context_seconds=0,
            interval_seconds=30,
        ),
    )


def make_due(db, job_id):
    with db.session() as session:
        job = session.get(Job, job_id)
        job.document = {**job.document, "retry_after": (datetime.now(UTC) - timedelta(seconds=1)).isoformat()}


def test_transient_retry_survives_restart_reuses_cache_and_preserves_order(db, sources, monkeypatch):
    plan = batch(db, sources[0])
    calls = []

    def interrupted(media, seconds):
        calls.append(seconds)
        if seconds == 30 and calls.count(seconds) == 1:
            raise TransientMediaError("Temporary video interruption")
        return fake_capture(media, seconds)

    monkeypatch.setattr(worker, "capture", interrupted)
    result = worker.run_one(db)
    assert result["state"] == "queued"
    job_id = result["id"]
    restarted = Database(db.settings)
    assert worker.run_one(restarted) is None  # Backoff blocks this batch's next unit as well.
    with db.session() as session:
        owner = session.get(ProcessingBatch, plan["id"])
        row = session.get(Job, job_id)
        execution = session.scalar(select(JobExecution))
        assert owner.state == "active" and owner.document["active_execution"] is None
        assert owner.document["spent_worker_seconds"] > 0
        assert row.attempts == 1 and row.document["retry_pending"]
        assert row.document["failure_history"][0]["automatic_retry"]
        assert execution.state == "failed"
        assert not list(session.scalars(select(Scan)))
    make_due(db, job_id)
    assert worker.run_one(restarted) == {"id": job_id, "state": "succeeded"}
    assert calls == [0, 30, 30]
    with db.session() as session:
        row = session.get(Job, job_id)
        assert row.attempts == 2 and row.document["cache_hits"] == 1
        assert not row.document["retry_pending"]
        assert len(list(session.scalars(select(Scan)))) == 1


def test_three_failed_attempts_pause_instead_of_retrying_forever(db, sources, monkeypatch):
    plan = batch(db, sources[0])

    def unavailable(*_):
        raise TransientMediaError("Recording unavailable")

    monkeypatch.setattr(worker, "capture", unavailable)
    for attempt, expected in enumerate(("queued", "queued", "failed"), 1):
        result = worker.run_one(db)
        assert result["state"] == expected
        with db.session() as session:
            row = session.get(Job, result["id"])
            assert row.attempts == attempt
            delay = (
                datetime.fromisoformat(row.document["retry_after"]) - datetime.now(UTC)
                if row.document["retry_after"]
                else None
            )
            if delay:
                assert delay.total_seconds() > (4 if attempt == 1 else 19)
        make_due(db, result["id"])
    assert worker.run_one(db) is None
    with db.session() as session:
        assert session.get(ProcessingBatch, plan["id"]).state == "paused"
        row = session.get(Job, result["id"])
        assert len(row.document["failure_history"]) == 3
        assert not row.document["retry_pending"]
        assert len(list(session.scalars(select(JobExecution)))) == 3


@pytest.mark.parametrize("action", ["pause", "cancel"])
def test_controls_stop_scheduled_retry_without_spending_an_attempt(db, sources, monkeypatch, action):
    plan = batch(db, sources[0])
    monkeypatch.setattr(worker, "capture", lambda *_: (_ for _ in ()).throw(TransientMediaError("temporary")))
    result = worker.run_one(db)
    control_batch(db, plan["id"], BatchControl(action=action))
    make_due(db, result["id"])
    assert worker.run_one(db) is None
    with db.session() as session:
        assert session.get(Job, result["id"]).attempts == 1
        assert session.get(ProcessingBatch, plan["id"]).state == (
            "paused" if action == "pause" else "cancelled"
        )


def test_invalid_evidence_is_not_automatically_retried(db, sources, monkeypatch):
    plan = batch(db, sources[0])
    monkeypatch.setattr(
        worker, "capture", lambda *_: (_ for _ in ()).throw(ValueError("Ambiguous timestamps"))
    )
    result = worker.run_one(db)
    assert result["state"] == "failed"
    with db.session() as session:
        assert session.get(ProcessingBatch, plan["id"]).state == "paused"
        assert not session.get(Job, result["id"]).document["retry_pending"]


@pytest.mark.parametrize(
    "uri,exception", [("https://fixture.invalid/video", TransientMediaError), ("local.mp4", ValueError)]
)
def test_capture_classifies_network_failures_only(monkeypatch, uri, exception):
    def unavailable(*_args, **_kwargs):
        raise subprocess.CalledProcessError(1, "ffmpeg")

    monkeypatch.setattr(frames.subprocess, "run", unavailable)
    with pytest.raises(exception) as error:
        frames.capture(uri, 27000)
    if exception is ValueError:
        assert not isinstance(error.value, TransientMediaError)


def test_expired_stream_is_resolved_again_on_retry(db, sources, monkeypatch):
    batch(db, sources[0])
    resolutions = []

    def resolve(_source, **_kwargs):
        resolutions.append(True)
        return {"uri": "expired" if len(resolutions) == 1 else "fresh", "format_id": "fixture"}

    def capture(uri, seconds):
        if uri == "expired":
            raise TransientMediaError("Expired remote stream")
        return fake_capture(uri, seconds)

    monkeypatch.setattr(worker, "resolve_media", resolve)
    monkeypatch.setattr(worker, "capture", capture)
    result = worker.run_one(db)
    make_due(db, result["id"])
    assert worker.run_one(db)["state"] == "succeeded"
    assert len(resolutions) == 2


@pytest.mark.usefixtures("fake_window")
def test_interrupted_dense_stream_retries_whole_window_without_partial_publication(db, sources, monkeypatch):
    original = window_jobs.decode_window
    calls = []

    def interrupted(*args, **kwargs):
        calls.append(True)
        stream = original(*args, **kwargs)
        if len(calls) == 1:
            try:
                yield next(stream)
            finally:
                stream.close()
            raise TransientMediaError("Incomplete remote window")
        yield from stream

    monkeypatch.setattr(window_jobs, "decode_window", interrupted)
    queued = window_jobs.enqueue_window(
        db,
        window_jobs.WindowRequest(
            source_id=sources[0]["id"], start_seconds=100, end_seconds=102, analyze_actors=False
        ),
    )
    assert worker.run_one(db)["state"] == "queued"
    with db.session() as session:
        assert not list(session.scalars(select(AnalysisWindow)))
        assert not list(session.scalars(select(Evidence)))
    make_due(db, queued["id"])
    assert worker.run_one(db)["state"] == "succeeded"
    with db.session() as session:
        windows = list(session.scalars(select(AnalysisWindow)))
        assert len(windows) == 1
        assert windows[0].document["sample_count"] == 4
        assert session.get(Job, queued["id"]).attempts == 2


def test_remote_incomplete_window_is_retryable_without_exposing_subprocess_output(tmp_path, monkeypatch):
    original = subprocess.Popen
    remote = "https://fixture.invalid/video"

    def unavailable(command, **kwargs):
        for option in remote_options(remote):
            command.remove(option)
        command = [str(tmp_path / "missing.mp4") if arg == remote else arg for arg in command]
        return original(command, **kwargs)

    monkeypatch.setattr(media_windows.subprocess, "Popen", unavailable)
    with pytest.raises(TransientMediaError, match="Window decoding was incomplete"):
        list(media_windows.decode_window(remote, 0, 1))


def test_automatic_retries_do_not_bypass_shared_processing_budget(db, sources, monkeypatch):
    plan = batch(db, sources[0])
    with db.session() as session:
        row = session.get(ProcessingBatch, plan["id"])
        row.document = {**row.document, "limits": {**row.document["limits"], "max_worker_seconds": 5}}
    clock = [0]
    monkeypatch.setattr(worker.time, "monotonic", lambda: clock[0])

    def fail(_db, _job):
        clock[0] += 6
        raise TransientMediaError("Temporary network interruption")

    monkeypatch.setattr(worker, "execute", fail)
    result = worker.run_one(db)
    assert result["state"] == "queued"
    make_due(db, result["id"])
    assert worker.run_one(db) is None
    with db.session() as session:
        owner = session.get(ProcessingBatch, plan["id"])
        assert owner.state == "paused" and owner.document["spent_worker_seconds"] == 6
        assert "worker-time budget" in owner.document["pause_reason"]
        assert session.get(Job, result["id"]).attempts == 1
