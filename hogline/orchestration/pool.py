"""Local process workers: independent compute, ordered work within each batch."""

import logging
import multiprocessing
import os
import shutil
import signal
import tempfile
import time
from dataclasses import replace
from pathlib import Path

from hogline.db import Database
from hogline.media.errors import configure_media_transport
from hogline.media.frames import cache_size, share_cache_usage
from hogline.orchestration.worker import run_one


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

    def start(self):
        root = self.settings.data_dir / "working-video"
        root.mkdir(parents=True, exist_ok=True)
        clean_abandoned_sections(root)
        self.directory = tempfile.TemporaryDirectory(prefix=f"pool-{os.getpid()}-", dir=root)
        settings = replace(self.settings, working_video_dir=Path(self.directory.name))
        share_cache_usage(self.settings.data_dir, self.cache_usage)
        for index in range(self.settings.workers):
            process = self.context.Process(
                target=worker_loop,
                args=(settings, self.stop, self.cache_usage),
                name=f"hogline-worker-{index + 1}",
                daemon=True,
            )
            process.start()
            self.processes.append(process)

    def status(self):
        alive = sum(p.is_alive() for p in self.processes)
        failed = [p for p in self.processes if p.exitcode is not None and p.exitcode != 0]
        return {
            "enabled": True,
            "running": alive > 0,
            "configured_workers": self.settings.workers,
            "alive_workers": alive,
            "media_transport": self.transport,
            "error": f"{len(failed)} processing workers stopped unexpectedly" if failed else None,
        }

    def close(self):
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
