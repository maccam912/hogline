"""Durable, bounded plans. A compute chunk is never a game."""

import math
from typing import Literal

from pydantic import Field, model_validator
from sqlalchemy import select

from hogline.adapters.sources import digest
from hogline.config import profile
from hogline.db import Job, JobExecution, ProcessingBatch, now
from hogline.domain.catalog import Conflict, source_or_error
from hogline.domain.models import Model, ScanRequest
from hogline.perception.stones import VERSION as DETECTOR_VERSION
from hogline.reconstruction.activity import VERSION


class PlanRequest(Model):
    source_ids: list[str] = Field(min_length=1, max_length=16)
    start_seconds: float = Field(default=0, ge=0)
    end_seconds: float | None = Field(default=None, gt=0)
    chunk_seconds: int = Field(default=600, ge=60, le=1080)
    context_seconds: int = Field(default=30, ge=0, le=60)
    interval_seconds: int = Field(default=10, ge=1, le=60)
    acquisition_tier: Literal["coarse", "evidence"] = "coarse"
    max_jobs: int = Field(default=12, ge=1, le=100)
    max_image_growth_mb: int = Field(default=250, ge=1, le=4096)
    max_worker_seconds: int = Field(default=1800, ge=5, le=86400)
    job_evidence_mb: int = Field(default=100, ge=1, le=512)
    job_worker_seconds: int = Field(default=600, ge=5, le=1800)

    @model_validator(mode="after")
    def valid(self):
        if len(set(self.source_ids)) != len(self.source_ids):
            raise ValueError("Select each source once")
        if self.end_seconds is not None and self.end_seconds <= self.start_seconds:
            raise ValueError("Plan end must follow start")
        if self.chunk_seconds % self.interval_seconds or self.context_seconds % self.interval_seconds:
            raise ValueError("Chunk and context lengths must be multiples of the sample interval")
        return self


def payload_for(source, request, layout=None):
    layout = layout or profile()
    return {
        "request": request.model_dump(),
        "stage_version": VERSION,
        "profile_sha256": digest(layout),
        "profile": layout,
        "source_hash": source["metadata_sha256"],
        "perception_version": DETECTOR_VERSION,
    }


def enqueue_in_session(session, payload, batch_id=None):
    key = digest(payload)
    job = session.scalar(select(Job).where(Job.job_key == key))
    if job is None:
        job = Job(job_key=key, document={**payload, "created_at": now(), "progress": 0, "batch_id": batch_id})
        session.add(job)
        session.flush()
    elif job.state == "cancelled" and job.attempts < 3:
        # Costs belong to immutable execution records, so a new plan may resume
        # cancelled logical work without erasing what an earlier batch spent.
        job.state = "queued"
        job.document = {**job.document, "batch_id": batch_id, "error": None}
    return job


def plan_in_session(session, request):
    layout, units, deferred, reused = profile(), [], 0, 0
    new_jobs, total_units, total_frames = 0, 0, 0
    existing = {j.job_key: j for j in session.scalars(select(Job))}
    for identifier in request.source_ids:
        source = source_or_error(session, identifier)
        duration = source.get("duration_seconds")
        if duration is None and request.end_seconds is None:
            raise ValueError("An unknown-duration source needs an explicit plan end")
        end = request.end_seconds if request.end_seconds is not None else duration
        if duration is not None:
            end = min(end, duration)
        if end <= request.start_seconds:
            raise ValueError("Plan start must fall before each selected recording's end")
        count = math.ceil((end - request.start_seconds) / request.chunk_seconds)
        if total_units + count > 10000:
            raise ValueError("Plan exceeds 10,000 work units; select a smaller date/source range")
        total_units += count
        for index in range(count):
            start = request.start_seconds + index * request.chunk_seconds
            stop = min(start + request.chunk_seconds, end)
            unit = ScanRequest(
                source_id=identifier,
                start_seconds=max(request.start_seconds, start - request.context_seconds),
                end_seconds=min(end, stop + request.context_seconds),
                ownership_start_seconds=start,
                ownership_end_seconds=stop,
                interval_seconds=request.interval_seconds,
                max_frames=1000,
                acquisition_tier=request.acquisition_tier,
                max_evidence_mb=request.job_evidence_mb,
                max_worker_seconds=request.job_worker_seconds,
            )
            frames = math.ceil((unit.end_seconds - unit.start_seconds) / unit.interval_seconds)
            payload = payload_for(source, unit, layout)
            job = existing.get(digest(payload))
            if job is not None and job.state == "succeeded":
                reused += 1
                units.append(
                    {
                        "payload": payload,
                        "job_id": job.id,
                        "disposition": "reuse_succeeded",
                        "sample_count": frames,
                    }
                )
            elif new_jobs < request.max_jobs:
                # Do not steal a queued/running job from another batch or a failed
                # execution's cost history. The plan reports these as blocked.
                disposition = (
                    "new"
                    if job is None
                    else (
                        "resume_cancelled"
                        if job.state == "cancelled" and job.attempts < 3
                        else "existing_needs_attention"
                    )
                )
                units.append(
                    {
                        "payload": payload,
                        "job_id": job.id if job else None,
                        "disposition": disposition,
                        "sample_count": frames,
                    }
                )
                new_jobs += 1
                total_frames += frames
            else:
                deferred += 1
    plan = {
        "request": request.model_dump(),
        "units": units,
        "total_requested_units": total_units,
        "admitted_new_units": new_jobs,
        "reused_succeeded_units": reused,
        "deferred_units": deferred,
        "estimated_new_sample_requests": total_frames,
        "image_growth_estimate_mb": round(
            total_frames * (0.20 if request.acquisition_tier == "coarse" else 2), 2
        ),
        "estimate_basis": "rough PNG allowance; not measured network bytes or a guarantee",
        "coverage_kind": "sparse_samples_only",
        "created_at": now(),
    }
    plan["sha256"] = digest({k: v for k, v in plan.items() if k != "created_at"})
    return plan


def preview_plan(db, request):
    with db.snapshot_session() as session:
        return plan_in_session(session, request)


class BatchStart(Model):
    request: PlanRequest
    expected_plan_sha256: str = Field(min_length=64, max_length=64)


def create_batch(db, request, expected_plan_sha256=None):
    with db.snapshot_session(write=True) as session:
        plan = plan_in_session(session, request)
        if expected_plan_sha256 is not None and plan["sha256"] != expected_plan_sha256:
            raise Conflict("The source or matching work changed since preview. Preview the plan again.")
        return publish_batch(session, plan)


def publish_batch(session, plan):
    if any(u["disposition"] == "existing_needs_attention" for u in plan["units"]):
        raise ValueError(
            "Matching unfinished work already exists. Resume/retry its owner before creating this plan."
        )
    batch = ProcessingBatch(
        document={
            **plan,
            "spent_worker_seconds": 0,
            "reserved_image_bytes": 0,
            "pause_reason": None,
            "active_execution": None,
            "active_executions": [],
            "parallelism": plan.get("parallelism", 1)
            if plan.get("kind") in ("ice_survey", "scorecard_read")
            else 1,
            "limits": {k: plan["request"][k] for k in ("max_image_growth_mb", "max_worker_seconds")},
            "control_history": [],
        }
    )
    session.add(batch)
    session.flush()
    units = []
    for unit in plan["units"]:
        job = enqueue_in_session(session, unit["payload"], batch.id)
        units.append({**unit, "job_id": job.id})
    batch.document = {
        **batch.document,
        "units": units,
        **(
            {
                "phase_history": [
                    {**phase, "job_ids": [u["job_id"] for u in units]} for phase in plan["phase_history"]
                ]
            }
            if plan.get("kind") == "automatic" and plan.get("phase_history")
            else {}
        ),
    }
    if plan.get("kind") != "automatic" and all(
        session.get(Job, u["job_id"]).state == "succeeded" for u in units
    ):
        batch.state = "completed"
        freeze_outcomes(session, batch)
    session.flush()
    return batch_view(session, batch)


def batch_view(session, batch):
    from hogline.orchestration.progress import analysis_progress

    doc = batch.document
    jobs = {u["job_id"]: session.get(Job, u["job_id"]) for u in doc["units"]}
    states = doc.get("unit_outcomes") or {identifier: job.state for identifier, job in jobs.items()}
    units = [
        {
            "job_id": u["job_id"],
            "state": states[u["job_id"]],
            "request": u["payload"]["request"],
            "kind": u["payload"].get("kind", "scan"),
            "disposition": u["disposition"],
            **({"selection": u["selection"]} if "selection" in u else {}),
        }
        for u in doc["units"]
    ]
    executions = list(session.scalars(select(JobExecution).where(JobExecution.batch_id == batch.id)))
    return {
        "id": batch.id,
        "revision": batch.revision,
        "state": batch.state,
        **{k: v for k, v in doc.items() if k not in ("units", "analysis_result")},
        "limits": doc.get(
            "limits", {k: doc["request"][k] for k in ("max_image_growth_mb", "max_worker_seconds")}
        ),
        "units": units,
        "succeeded_units": sum(s == "succeeded" for s in states.values()),
        "executions": [{"id": e.id, "job_id": e.job_id, "state": e.state, **e.document} for e in executions],
        "progress_summary": analysis_progress(doc, units, jobs, executions, batch.state),
    }


def batches(db):
    with db.session() as session:
        return batches_in_session(session)


def batches_in_session(session):
    return [batch_view(session, b) for b in session.scalars(select(ProcessingBatch))]


class BatchControl(Model):
    action: Literal["pause", "resume", "cancel"]
    max_image_growth_mb: int | None = Field(default=None, ge=1, le=4096)
    max_worker_seconds: int | None = Field(default=None, ge=5, le=86400)
    retry_failed: bool = False
    parallelism: int | None = Field(default=None, ge=1, le=8)


def freeze_outcomes(session, batch):
    batch.document = {
        **batch.document,
        "unit_outcomes": {u["job_id"]: session.get(Job, u["job_id"]).state for u in batch.document["units"]},
    }


def control_batch(db, identifier, control):
    with db.snapshot_session(write=True) as session:
        batch = session.get(ProcessingBatch, identifier)
        if batch is None:
            raise ValueError("Batch not found")
        if batch.state in ("completed", "cancelled"):
            raise ValueError(
                "Completed/cancelled batches are immutable; preview a new plan for remaining work"
            )
        if control.parallelism is not None and batch.document.get("kind") not in (
            "ice_survey",
            "scorecard_read",
        ):
            raise ValueError(
                "Parallel units are supported only for independent ice or retained scorecard tasks"
            )
        limits = dict(batch.document.get("limits", batch.document["request"]))
        for key in ("max_image_growth_mb", "max_worker_seconds"):
            if getattr(control, key) is not None:
                limits[key] = getattr(control, key)
        if control.action == "resume":
            if batch.document["reserved_image_bytes"] >= limits["max_image_growth_mb"] * 1024**2:
                raise ValueError("Raise the image-growth budget before resuming")
            if batch.document["spent_worker_seconds"] >= limits["max_worker_seconds"]:
                raise ValueError("Raise the worker-time budget before resuming")
            for unit in batch.document["units"]:
                job = session.get(Job, unit["job_id"])
                if job.state == "failed":
                    if not control.retry_failed:
                        raise ValueError("Retry failed units before resuming this batch")
                    if job.attempts >= 3:
                        raise ValueError("A failed job reached its retry limit; start a new analysis")
                    job.state = "queued"
                    job.document = {**job.document, "error": None, "progress": 0}
            batch.state = "active"
        else:
            batch.state = "paused" if control.action == "pause" else "cancelled"
            if control.action == "cancel":
                for unit in batch.document["units"]:
                    job = session.get(Job, unit["job_id"])
                    if job.state in ("queued", "failed") and job.document.get("batch_id") == batch.id:
                        job.state = "cancelled"
        batch.document = {
            **batch.document,
            "parallelism": control.parallelism or batch.document.get("parallelism", 1),
            "limits": limits,
            "control_history": [
                *batch.document.get("control_history", []),
                {
                    "at": now(),
                    "action": control.action,
                    "limits": limits,
                    "parallelism": control.parallelism or batch.document.get("parallelism", 1),
                },
            ],
            "pause_reason": "operator" if control.action != "resume" else None,
        }
        if batch.state == "cancelled" and not batch.document.get("active_execution"):
            freeze_outcomes(session, batch)
        session.flush()
        return batch_view(session, batch)


class WorkStopped(ValueError):
    pass


def execution_tick(session, execution, elapsed):
    doc = execution.document
    delta = max(0, elapsed - doc.get("wall_seconds", 0))
    execution.document = {
        **doc,
        "wall_seconds": max(elapsed, doc.get("wall_seconds", 0)),
        "updated_at": now(),
    }
    if execution.batch_id:
        batch = session.get(ProcessingBatch, execution.batch_id)
        batch.document = {
            **batch.document,
            "spent_worker_seconds": batch.document["spent_worker_seconds"] + delta,
        }
        return batch


def check_execution_in_session(session, token, elapsed, reserve_bytes=0):
    """Use the caller's transaction when checking publication; never open a second writer."""
    stopped = None
    execution = session.get(JobExecution, token)
    if execution is None or execution.state != "running":
        raise WorkStopped("Execution lease is no longer active")
    job = session.get(Job, execution.job_id)
    if job.state != "running" or job.document.get("lease_token") != token:
        raise WorkStopped("Work lease was reclaimed")
    batch = execution_tick(session, execution, elapsed)
    if batch:
        limits = batch.document.get("limits", batch.document["request"])
        if batch.state != "active":
            stopped = f"Batch {batch.state}"
        elif batch.document["spent_worker_seconds"] >= limits["max_worker_seconds"]:
            stopped = "Batch worker-time budget reached"
        elif batch.document["reserved_image_bytes"] + reserve_bytes > limits["max_image_growth_mb"] * 1024**2:
            stopped = "Batch image-growth budget reached"
        if stopped and batch.state == "active":
            batch.state = "paused"
            batch.document = {**batch.document, "pause_reason": stopped}
        if not stopped and reserve_bytes:
            batch.document = {
                **batch.document,
                "reserved_image_bytes": batch.document["reserved_image_bytes"] + reserve_bytes,
            }
    if not stopped and reserve_bytes:
        execution.document = {
            **execution.document,
            "new_image_bytes_reserved": execution.document.get("new_image_bytes_reserved", 0) + reserve_bytes,
        }
    return stopped


def check_execution(db, token, elapsed, reserve_bytes=0):
    with db.snapshot_session(write=True) as session:
        stopped = check_execution_in_session(session, token, elapsed, reserve_bytes)
    if stopped:
        raise WorkStopped(stopped)


def finish_execution(session, token, state, elapsed=None, *, will_retry=False):
    execution = session.get(JobExecution, token)
    if execution is None or execution.state != "running":
        return
    batch = execution_tick(
        session, execution, elapsed if elapsed is not None else execution.document.get("wall_seconds", 0)
    )
    execution.state = state
    execution.document = {**execution.document, "finished_at": now()}
    if batch:
        remaining = [t for t in active_tokens(batch.document) if t != token]
        batch.document = {
            **batch.document,
            "active_execution": remaining[0] if remaining else None,
            "active_executions": remaining,
        }
        if state == "failed" and batch.state == "active" and not will_retry:
            batch.state = "paused"
            batch.document = {**batch.document, "pause_reason": "A work unit failed; inspect and retry it"}
        if (
            batch.state == "active"
            and (
                batch.document.get("kind") != "automatic"
                or (
                    batch.document.get("phase") == "ocr"
                    and batch.document.get("campaign_purpose") != "board_change_refinement"
                )
            )
            and all(session.get(Job, u["job_id"]).state == "succeeded" for u in batch.document["units"])
        ):
            batch.state = "completed"
        if batch.state in ("completed", "cancelled"):
            freeze_outcomes(session, batch)


def active_tokens(document):
    return document.get("active_executions") or (
        [document["active_execution"]] if document.get("active_execution") else []
    )
