import multiprocessing
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime
from fractions import Fraction

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from hogline.adapters.sources import import_local
from hogline.db import Database, Job, JobExecution
from hogline.media import chunks
from hogline.media.frames import EvidenceBudget, EvidenceLimitError, capture, share_cache_usage
from hogline.orchestration.plans import PlanRequest, WorkStopped, create_batch
from hogline.orchestration.pool import WorkerPool, clean_abandoned_sections
from hogline.orchestration.worker import claim
from hogline.review.app import create_app


def test_concurrent_claims_keep_one_execution_per_batch(db, sources):
    for source in sources:
        create_batch(
            db, PlanRequest(source_ids=[source["id"]], end_seconds=120, chunk_seconds=60, context_seconds=0)
        )
    with ThreadPoolExecutor(max_workers=8) as executor:
        claimed = list(executor.map(lambda _: claim(Database(db.settings)), range(8)))
    claimed = [j for j in claimed if j]
    assert len(claimed) == 2
    assert len({j["batch_id"] for j in claimed}) == 2
    with db.session() as session:
        assert len(list(session.scalars(select(JobExecution)))) == 2


def test_pool_processes_finish_real_local_work_and_clean_working_space(db, video):
    longer = video.with_name("longer.mp4")
    subprocess.run(
        ["ffmpeg", "-v", "error", "-stream_loop", "9", "-i", str(video), "-c", "copy", str(longer)],
        check=True,
    )
    source = import_local(db, str(longer), 5)
    from hogline.domain.models import ScanRequest
    from hogline.orchestration.worker import enqueue

    for start in (0, 15):
        enqueue(
            db,
            ScanRequest(
                source_id=source["id"],
                start_seconds=start,
                end_seconds=start + 14,
                interval_seconds=1,
                acquisition_tier="coarse",
            ),
        )
    pool = WorkerPool(replace(db.settings, workers=2))
    try:
        pool.start()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            assert pool.status()["alive_workers"] == 2, pool.status()
            with db.session() as session:
                states = [j.state for j in session.scalars(select(Job))]
            if states == ["succeeded", "succeeded"]:
                break
            assert "failed" not in states
            time.sleep(0.1)
        assert states == ["succeeded", "succeeded"]
        with db.session() as session:
            executions = list(session.scalars(select(JobExecution)))
            starts = [datetime.fromisoformat(e.document["started_at"]) for e in executions]
            finishes = [datetime.fromisoformat(e.document["finished_at"]) for e in executions]
        assert max(starts) < min(finishes)  # Actual execution overlaps across processes.
    finally:
        pool.close()
    assert not list((db.settings.data_dir / "working-video").iterdir())
    assert pool.status()["alive_workers"] == 0


def test_shared_cache_reservations_reject_combined_growth(db):
    counter = multiprocessing.get_context("spawn").Value("q", 0)
    share_cache_usage(db.settings.data_dir, counter)
    try:
        first = EvidenceBudget(db.settings.data_dir, 2, 1)
        second = EvidenceBudget(db.settings.data_dir, 2, 1)
        first.consume(700_000, 700_000)
        with pytest.raises(EvidenceLimitError, match="Shared evidence cache"):
            second.consume(700_000, 700_000)
        assert counter.value == 700_000
        second.consume(700_000, 0)  # Existing evidence costs no new cache growth.
        first.reserve_growth = lambda _: (_ for _ in ()).throw(ValueError("stopped"))
        with pytest.raises(ValueError, match="stopped"):
            first.consume(1, 1)
        assert counter.value == 700_000
    finally:
        share_cache_usage(db.settings.data_dir, None)


@pytest.mark.parametrize("offset", [0, 7])
def test_video_chunk_matches_original_pts_and_pixels_and_cleans_up(db, video, monkeypatch, offset):
    shifted = video.with_name("shifted.mp4")
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-stream_loop",
            "3",
            "-i",
            str(video),
            "-c",
            "libx264",
            "-g",
            "10",
            "-bf",
            "0",
            "-output_ts_offset",
            str(offset),
            str(shifted),
        ],
        check=True,
    )
    original_probe, original_popen = chunks.probe_stream, subprocess.Popen
    remote = "https://fixture.invalid/video"
    monkeypatch.setattr(
        chunks, "probe_stream", lambda uri: original_probe(str(shifted) if uri == remote else uri)
    )

    def local_download(command, **kwargs):
        command = [str(shifted) if argument == remote else argument for argument in command]
        return original_popen(command, **kwargs)

    monkeypatch.setattr(chunks.subprocess, "Popen", local_download)
    root = db.settings.data_dir / "working-video"
    ticks = []
    with chunks.VideoChunk(root, 8) as chunk:
        chunk.prepare(remote, 4.8, 8.8, lambda: ticks.append(True))
        assert chunk.path is not None
        assert float(chunk.origin) >= offset + 4
        for seconds in (4.8, 5.2, 8.6):
            expected, timing = capture(str(shifted), seconds)
            actual, actual_timing = chunk.capture(capture, remote, seconds)
            assert actual_timing == timing
            assert actual.tobytes() == expected.tobytes()
        assert ticks
    assert not list(root.iterdir())


def test_failed_chunk_falls_back_and_cancellation_cleans_up(db, monkeypatch):
    root = db.settings.data_dir / "working-video"
    monkeypatch.setattr(chunks, "probe_stream", lambda _: (_ for _ in ()).throw(ValueError("unavailable")))
    with chunks.VideoChunk(root, 8) as chunk:
        chunk.prepare("https://fixture.invalid/video", 0, 10, lambda: None)
        assert chunk.path is None
        assert chunk.capture(lambda uri, seconds: (uri, seconds), "original", 3) == ("original", 3)
    assert not list(root.iterdir())

    def stopped():
        raise ValueError("cancelled")

    with pytest.raises(ValueError, match="cancelled"), chunks.VideoChunk(root, 8) as chunk:
        chunk.prepare("https://fixture.invalid/video", 0, 10, stopped)
    assert not list(root.iterdir())


def test_catalog_reports_pool_capacity(db):
    with TestClient(create_app(replace(db.settings, workers=8))) as client:
        status = client.get("/api/catalog").json()["worker"]
        assert status["configured_workers"] == 8
        assert status["alive_workers"] == 0
        assert not status["running"]


def test_cancel_during_download_kills_decoder_and_removes_section(db, monkeypatch):
    original_popen = subprocess.Popen
    children = []

    def process(*_args, **_kwargs):
        child = original_popen([sys.executable, "-c", "import time; time.sleep(30)"])
        children.append(child)
        return child

    monkeypatch.setattr(chunks, "probe_stream", lambda _: (Fraction(1, 1000), 0, 1_000_000))
    monkeypatch.setattr(chunks.subprocess, "Popen", process)
    calls = []

    def cancel():
        calls.append(True)
        if len(calls) > 1:
            raise WorkStopped("cancelled")

    root = db.settings.data_dir / "working-video"
    with pytest.raises(WorkStopped, match="cancelled"), chunks.VideoChunk(root, 8) as chunk:
        chunk.prepare("https://fixture.invalid/video", 0, 60, cancel)
    assert children and all(p.poll() is not None for p in children)
    assert not list(root.iterdir())


def test_abandoned_sections_cleaned_without_touching_live_pool(db, monkeypatch):
    root = db.settings.data_dir
    for name in ("pool-123-dead", "pool-456-live"):
        directory = root / name
        directory.mkdir()
        (directory / "section.nut").write_bytes(b"video")

    def alive(pid, _):
        if pid == 123:
            raise ProcessLookupError()

    monkeypatch.setattr("hogline.orchestration.pool.os.kill", alive)
    clean_abandoned_sections(root)
    assert not (root / "pool-123-dead").exists()
    assert (root / "pool-456-live" / "section.nut").exists()
