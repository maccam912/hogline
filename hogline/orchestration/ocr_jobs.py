"""Retained-frame OCR with durable leases and shared campaign accounting."""

import time

from hogline.adapters.sources import digest
from hogline.db import Evidence, Job, ProcessingBatch, now
from hogline.domain.catalog import source_or_error
from hogline.media.frames import EvidenceBudget
from hogline.orchestration.plans import WorkStopped, check_execution, finish_execution
from hogline.perception.broadcast import analyze_broadcast_frame, broadcast_context
from hogline.perception.ocr import OCRBudgetReached, TesseractOCR


def execute_ocr(db, job):
    from hogline.orchestration.worker import update_owned

    started, token = time.monotonic(), job["lease_token"]
    check_execution(db, token, 0)
    with db.session() as session:
        source = source_or_error(session, job["request"]["source_id"])
        frame = session.get(Evidence, job["evidence_id"])
        context = broadcast_context(session, db.settings.data_dir, job["profile"])
        if (
            source["metadata_sha256"] != job["source_hash"]
            or frame is None
            or digest(frame.document) != job["evidence_sha256"]
        ):
            raise ValueError("OCR source or evidence changed; preview current work")
        if context["version"] != job["stage_version"]:
            raise ValueError("OCR method or retained header reference changed; preview current work")
        batch = session.get(ProcessingBatch, job["batch_id"]) if job.get("batch_id") else None
        remaining = (
            batch.document["limits"]["max_worker_seconds"] - batch.document["spent_worker_seconds"]
            if batch
            else job["request"]["max_worker_seconds"]
        )
    seconds = min(remaining, job["request"]["max_worker_seconds"])
    reader = TesseractOCR(deadline=started + seconds)
    if not reader.available:
        raise ValueError("Install Tesseract OCR to read retained frames")
    budget = EvidenceBudget(db.settings.data_dir, job["request"]["max_evidence_mb"], db.settings.max_cache_mb)
    budget.reserve_growth = lambda size: check_execution(db, token, time.monotonic() - started, size)
    try:
        with db.session() as session:
            observation = analyze_broadcast_frame(
                session, frame, db.settings.data_dir, job["profile"], reader, budget, context=context
            )
    except OCRBudgetReached as exc:
        if remaining <= job["request"]["max_worker_seconds"]:
            raise WorkStopped("Campaign worker-time budget reached during OCR") from exc
        raise ValueError("One frame exceeded its OCR time allowance") from exc
    check_execution(db, token, time.monotonic() - started)
    update_owned(db, job["id"], token, progress=1, completed_frames=1, observation_id=observation["id"])
    with db.snapshot_session(write=True) as session:
        row = session.get(Job, job["id"])
        if row.state != "running" or row.document.get("lease_token") != token:
            raise WorkStopped("OCR lease was reclaimed")
        row.state, row.lease_until = "succeeded", None
        row.document = {**row.document, "finished_at": now(), "wall_seconds": time.monotonic() - started}
        finish_execution(session, token, "succeeded", time.monotonic() - started)
    return observation
