"""Bounded recovery for unattended recordings; failures never count as finished analysis."""

from sqlalchemy import select

from hogline.db import Job, ProcessingBatch, now, queue_field
from hogline.orchestration.plans import active_tokens, enqueue_in_session, freeze_outcomes

MAX_TIMEOUT_REPLACEMENTS = 3


def recover_recordings(session):
    for batch in session.scalars(
        select(ProcessingBatch).where(
            ProcessingBatch.state == "paused",
            queue_field(ProcessingBatch, "campaign_purpose") == "recording_analysis",
        )
    ):
        doc = batch.document
        # Explicit pauses are intentional. Let sibling workers stop before changing admission or files.
        if doc.get("pause_reason") == "operator" or active_tokens(doc):
            continue
        failed = [session.get(Job, u["job_id"]) for u in doc["units"]]
        failed = [j for j in failed if j.state == "failed"]
        history = doc.get("recovery_history", [])
        limits = doc["limits"]
        within_limits = (
            doc["spent_worker_seconds"] < limits["max_worker_seconds"]
            and doc["reserved_image_bytes"] < limits["max_image_growth_mb"] * 1024**2
        )
        recoverable = failed and all(
            j.document.get("kind") == "dense_window"
            and (j.document.get("error") or "").startswith("Detailed window worker-time budget reached")
            and not j.document.get("recovery_of")
            and j.document["request"]["max_worker_seconds"] < 600
            for j in failed
        )
        if within_limits and recoverable and len(history) + len(failed) <= MAX_TIMEOUT_REPLACEMENTS:
            try:
                with session.begin_nested():
                    retry_timeouts(session, batch, failed)
                continue
            except ValueError:
                pass  # A conflicting replacement key must not strand half a recovery.
        # Keep measurements, provenance and failure details; cancel only work this analysis owns.
        for unit in doc["units"]:
            job = session.get(Job, unit["job_id"])
            if job.state == "queued" and job.document.get("batch_id") == batch.id:
                job.state = "cancelled"
        batch.state = "failed"
        batch.document = {
            **doc,
            "finished_at": now(),
            "analysis_outcome": "needs_attention",
            "recovery_history": [
                *history,
                {
                    "at": now(),
                    "action": "continue_queue",
                    "reason": doc.get("pause_reason"),
                    "failed_jobs": [{"id": j.id, "error": j.document.get("error")} for j in failed],
                },
            ],
        }
        session.flush()
        freeze_outcomes(session, batch)


def retry_timeouts(session, batch, failed):
    doc, replacements = batch.document, {}
    history = doc.get("recovery_history", [])
    for job in failed:
        unit = next(u for u in doc["units"] if u["job_id"] == job.id)
        payload = {
            **unit["payload"],
            "request": {
                **unit["payload"]["request"],
                "max_worker_seconds": min(600, job.document["request"]["max_worker_seconds"] * 2),
            },
        }
        replacement = enqueue_in_session(session, payload, batch.id)
        if replacement.document.get("batch_id") != batch.id or replacement.state not in (
            "queued",
            "succeeded",
        ):
            raise ValueError("Recovery work belongs to another analysis or already failed")
        replacement.document = {**replacement.document, "recovery_of": job.id}
        replacements[job.id] = {**unit, "payload": payload, "job_id": replacement.id}
        history = [
            *history,
            {
                "at": now(),
                "action": "retry_timeout",
                "failed_job_id": job.id,
                "replacement_job_id": replacement.id,
                "max_worker_seconds": payload["request"]["max_worker_seconds"],
            },
        ]
    batch.state = "active"
    batch.document = {
        **doc,
        "pause_reason": None,
        "recovery_history": history,
        "units": [replacements.get(u["job_id"], u) for u in doc["units"]],
        "phase_history": [
            {**p, "job_ids": [replacements[j]["job_id"] if j in replacements else j for j in p["job_ids"]]}
            for p in doc["phase_history"]
        ],
    }
