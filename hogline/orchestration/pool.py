"""Local process workers: independent compute, ordered work within each batch."""

import logging
import multiprocessing
import os
import shutil
import signal
import tempfile
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select

from hogline.db import Database, Job, JobExecution, now
from hogline.media.errors import configure_media_transport
from hogline.media.frames import cache_size, share_cache_usage
from hogline.orchestration.worker import run_one


def stalled_workers(db, current=None):
    """A refreshed lease means liveness; a separate hard deadline bounds endless heartbeats."""
    current = current or datetime.now(UTC)
    stalled = set()
    with db.snapshot_session() as session:
        for job in session.scalars(select(Job).where(Job.state == "running")):
            execution = session.get(JobExecution, job.document.get("lease_token", ""))
            if execution is None or execution.state != "running":
                continue
            pid = execution.document.get("worker_pid")
            started = datetime.fromisoformat(execution.document["started_at"])
            deadline = job.document["request"].get("max_worker_seconds", 300) + 120
            expired = job.lease_until and datetime.fromisoformat(job.lease_until) < current
            if pid and (expired or (current - started).total_seconds() > deadline):
                stalled.add(pid)
    return stalled


def clean_abandoned_sections(root):
    for directory in root.glob("pool-*"):
        try:
            pid = int(directory.name.split("-", 2)[1])
            os.kill(pid, 0)
        except ProcessLookupError:
            shutil.rmtree(directory)
        except (ValueError, PermissionError):
            continue


def worker_loop(settings, stop, cache_usage):
    import cv2

    cv2.setNumThreads(1)
    configure_media_transport()

    def terminate(_signal, _frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, terminate)
    os.environ.setdefault("OMP_THREAD_LIMIT", "1")
    share_cache_usage(settings.data_dir, cache_usage)
    db = Database(settings)
    try:
        while not stop.is_set():
            if run_one(db) is None:
                stop.wait(1)
    except Exception:
        logging.getLogger(__name__).exception("Processing worker stopped")
        raise
    finally:
        db.engine.dispose()


class WorkerPool:
    def __init__(self, settings):
        self.settings = settings
        self.transport = configure_media_transport()
        self.context = multiprocessing.get_context("spawn")
        self.stop = self.context.Event()
        self.cache_usage = self.context.Value("q", cache_size(settings.data_dir))
        self.processes = []
        self.directory = None
        self.monitor = None
        self.monitor_stop = threading.Event()
        self.restarts = 0
        self.error = None

    def start(self):
        root = self.settings.data_dir / "working-video"
        root.mkdir(parents=True, exist_ok=True)
        clean_abandoned_sections(root)
        self.directory = tempfile.TemporaryDirectory(prefix=f"pool-{os.getpid()}-", dir=root)
        self.worker_settings = replace(self.settings, working_video_dir=Path(self.directory.name))
        share_cache_usage(self.settings.data_dir, self.cache_usage)
        for index in range(self.settings.workers):
            self.processes.append(self.spawn(index))
        self.monitor = threading.Thread(target=self.supervise, daemon=True, name="hogline-worker-supervisor")
        self.monitor.start()

    def spawn(self, index):
        directory = self.worker_settings.working_video_dir / f"worker-{index + 1}"
        directory.mkdir(parents=True, exist_ok=True)
        process = self.context.Process(
            target=worker_loop,
            args=(replace(self.worker_settings, working_video_dir=directory), self.stop, self.cache_usage),
            name=f"hogline-worker-{index + 1}",
            daemon=True,
        )
        process.start()
        return process

    def supervise(self):
        db = Database(self.settings)
        try:
            while not self.monitor_stop.wait(5):
                try:
                    self.maintain(db)
                    self.error = None
                except Exception as exc:
                    self.error = str(exc)
                    logging.getLogger(__name__).exception("Worker supervision will retry")
        finally:
            db.engine.dispose()

    def maintain(self, db):
        stalled = stalled_workers(db)
        for index, process in enumerate(self.processes):
            if self.monitor_stop.is_set():
                return
            if process.is_alive() and process.pid not in stalled:
                continue
            if process.is_alive():
                process.terminate()
                process.join(timeout=2)
                if process.is_alive():
                    process.kill()
            process.join(timeout=2)
            if process.is_alive():
                continue
            # Fence the old process before allowing its work to be reclaimed.
            with db.snapshot_session(write=True) as session:
                for job in session.scalars(select(Job).where(Job.state == "running")):
                    execution = session.get(JobExecution, job.document.get("lease_token", ""))
                    if execution and execution.document.get("worker_pid") == process.pid:
                        job.lease_until = now()
                        execution.document = {
                            **execution.document,
                            "worker_restarted_at": now(),
                            "restart_reason": "stalled" if process.pid in stalled else "exited",
                        }
            shutil.rmtree(self.worker_settings.working_video_dir / f"worker-{index + 1}", ignore_errors=True)
            self.processes[index] = self.spawn(index)
            self.restarts += 1

    def status(self):
        alive = sum(p.is_alive() for p in self.processes)
        failed = [p for p in self.processes if p.exitcode is not None and p.exitcode != 0]
        return {
            "enabled": True,
            "running": alive > 0,
            "configured_workers": self.settings.workers,
            "recording_slots": self.settings.recording_slots,
            "recording_parallelism": self.settings.recording_parallelism,
            "alive_workers": alive,
            "media_transport": self.transport,
            "worker_restarts": self.restarts,
            "error": self.error or (f"{len(failed)} processing workers awaiting restart" if failed else None),
        }

    def close(self):
        self.monitor_stop.set()
        if self.monitor:
            self.monitor.join(timeout=10)
        self.stop.set()
        deadline = time.monotonic() + 5
        for process in self.processes:
            process.join(timeout=max(0, deadline - time.monotonic()))
        for process in self.processes:
            if process.is_alive():
                process.terminate()
            process.join(timeout=1)
        share_cache_usage(self.settings.data_dir, None)
        if self.directory:
            self.directory.cleanup()
