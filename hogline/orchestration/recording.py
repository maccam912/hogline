"""A recording-wide, resumable survey → board → detail → reconstruction pipeline."""

import json
import math
import time
from datetime import datetime, timedelta

from pydantic import Field
from sqlalchemy import func, select

from hogline.adapters.sources import digest
from hogline.analytics.end_timeline import source_end_timeline
from hogline.config import profile
from hogline.db import AnalysisWindow, Evidence, Job, ProcessingBatch, Scan, now, queue_field
from hogline.domain.catalog import source_or_error
from hogline.domain.models import Model, uid
from hogline.media.frames import STORAGE_VERSION
from hogline.media.post_live import has_verified_download
from hogline.media.retention import POLICY
from hogline.orchestration.adaptive import AdaptiveRequest, adaptive_in_session
from hogline.orchestration.automatic import AutomaticRequest, append_phase, method_pin, ocr_units
from hogline.orchestration.plans import (
    PlanRequest,
    check_execution,
    check_execution_in_session,
    enqueue_in_session,
    finish_execution,
    freeze_outcomes,
    plan_in_session,
    publish_batch,
)
from hogline.orchestration.windows import WindowRequest, window_payload
from hogline.perception.layout import validate_reference_assets
from hogline.reconstruction.ending_windows import VERSION as ENDING_VERSION
from hogline.reconstruction.ending_windows import ending_windows

VERSION = "whole-recording-analysis-v2"
PHASES = ["survey", "boards", "detail", "detail_boards", "reconstruct"]


class RecordingAnalysisRequest(Model):
    max_worker_seconds: int | None = Field(default=None, ge=60, le=86400)
    max_image_growth_mb: int | None = Field(default=None, ge=32, le=4096)
    detail_windows: int | None = Field(default=None, ge=1, le=32)


def recording_plan(session, source_id, request, parallelism=4):
    source = source_or_error(session, source_id)
    duration = source.get("duration_seconds")
    if not duration or not math.isfinite(duration) or duration <= 0:
        raise ValueError("Complete the recording metadata before starting full-recording analysis")
    if duration > 86400:
        raise ValueError("This pilot analyzes recordings up to 24 hours; split longer recordings first")
    if source.get("live_status") in ("is_live", "is_upcoming") or (
        source.get("live_status") == "post_live" and not has_verified_download(source)
    ):
        raise ValueError(
            "YouTube has not finished this live stream yet; refresh its metadata after it has been processed"
        )
    validate_reference_assets(profile())
    details = request.detail_windows or min(32, max(4, math.ceil(duration / 1800) * 3))
    frames = math.ceil(duration / 60)
    growth = request.max_image_growth_mb or min(4096, max(128, math.ceil(frames * 2.5 + details * 24)))
    seconds = request.max_worker_seconds or min(86400, max(600, math.ceil(frames * 8 + details * 90)))
    plan = plan_in_session(
        session,
        PlanRequest(
            source_ids=[source_id],
            start_seconds=0,
            end_seconds=duration,
            chunk_seconds=1080,
            context_seconds=0,
            interval_seconds=60,
            acquisition_tier="evidence",
            max_jobs=100,
            max_image_growth_mb=growth,
            max_worker_seconds=seconds,
            job_evidence_mb=64,
            job_worker_seconds=300,
        ),
    )
    repair_run = None
    if source.get("image_retention", {}).get("routine_images_released"):
        repair_run = uid()
        for unit in plan["units"]:
            unit.update(payload={**unit["payload"], "repair_run": repair_run}, job_id=None, disposition="new")
        plan["reused_succeeded_units"] = 0
    if plan["deferred_units"]:
        raise ValueError("The full recording did not fit the survey plan")
    return {
        **plan,
        "kind": "automatic",
        "campaign_purpose": "recording_analysis",
        "storage_policy": POLICY,
        "repair_run": repair_run,
        "parallelism": parallelism,
        "recording_analysis_version": VERSION,
        "ending_inspection_version": ENDING_VERSION,
        "phase": "survey",
        "phase_history": [],
        "method_pin": method_pin(),
        "source_hashes": {source_id: source["metadata_sha256"]},
        "request": {
            "source_ids": [source_id],
            "start_seconds": 0,
            "end_seconds": duration,
            "max_image_growth_mb": growth,
            "max_worker_seconds": seconds,
            "detail_windows": details,
        },
        "coverage_kind": "full_duration_sparse_survey_with_selected_dense_detail",
        "notice": "The entire recording is surveyed; selected moments receive detailed analysis. "
        "Games, ends and scores remain proposals where evidence is uncertain.",
    }


def start_recording_analysis(db, source_id, request):
    with db.snapshot_session(write=True) as session:
        source_or_error(session, source_id)
        for batch in session.scalars(
            select(ProcessingBatch).where(
                ProcessingBatch.state.in_(["active", "paused"]),
                ProcessingBatch.document["campaign_purpose"].as_string() == "recording_analysis",
                ProcessingBatch.document["request"]["source_ids"][0].as_string() == source_id,
            )
        ):
            if batch.document.get("campaign_purpose") == "recording_analysis" and batch.document["request"][
                "source_ids"
            ] == [source_id]:
                from hogline.orchestration.plans import batch_view

                return {**batch_view(session, batch), "already_exists": True}
        plan = recording_plan(session, source_id, request, db.settings.recording_parallelism)
        plan["sha256"] = digest({k: v for k, v in plan.items() if k not in ("sha256", "created_at")})
        return {**publish_batch(session, plan), "already_exists": False}


def restart_failed_recording(db, identifier):
    """Explicit operator retry: retain failed attempts and reacquire released prerequisites when needed."""
    with db.snapshot_session(write=True) as session:
        batch = session.get(ProcessingBatch, identifier)
        if (
            batch is None
            or batch.state != "failed"
            or batch.document.get("campaign_purpose") != "recording_analysis"
        ):
            raise ValueError("Only a failed recording analysis can be restarted")
        doc = batch.document
        if doc.get("active_execution"):
            raise ValueError("Stop the recording's workers before restarting it")
        jobs = {u["job_id"]: session.get(Job, u["job_id"]) for u in doc["units"]}
        failed = [j for j in jobs.values() if j.state == "failed"]
        history = {
            "at": now(),
            "created_at": doc["created_at"],
            "phase": doc["phase"],
            "reason": doc.get("pause_reason"),
            "unit_outcomes": {i: j.state for i, j in jobs.items()},
            "spent_worker_seconds": doc["spent_worker_seconds"],
            "reserved_image_bytes": doc["reserved_image_bytes"],
            "failed_job_ids": [j.id for j in failed],
            "phase_history": doc.get("phase_history", []),
        }
        reconstruction_only = doc["phase"] == "reconstruct" and all(
            j.state == "succeeded" or j.document.get("kind") == "recording_reconstruction"
            for j in jobs.values()
        )
        clean = {
            k: v
            for k, v in doc.items()
            if k
            not in (
                "finished_at",
                "unit_outcomes",
                "image_cleanup",
                "analysis_outcome",
                "analysis_result",
                "result_sha256",
                "result_summary",
                "repair_run",
            )
        }
        clean.update(
            {
                "pause_reason": None,
                "active_execution": None,
                "active_executions": [],
                "spent_worker_seconds": 0,
                "reserved_image_bytes": 0,
                "recovery_history": [],
                "retry_started_at": now(),
                "restart_history": [*doc.get("restart_history", []), history],
            }
        )
        # Finish already-open caches before a restarted recording opens another workspace.
        last_open = session.scalar(
            select(func.max(queue_field(ProcessingBatch, "created_at"))).where(
                ProcessingBatch.state == "active",
                queue_field(ProcessingBatch, "campaign_purpose") == "recording_analysis",
                ProcessingBatch.document["spent_worker_seconds"].as_float() > 0,
            )
        )
        if last_open and last_open >= doc["created_at"]:
            clean["created_at"] = (datetime.fromisoformat(last_open) + timedelta(microseconds=1)).isoformat()
        if reconstruction_only:
            for job in jobs.values():
                if job.state != "succeeded":
                    job.state, job.attempts, job.lease_until = "queued", 0, None
                    job.document = {
                        **job.document,
                        "error": None,
                        "progress": 0,
                        "retry_pending": False,
                        "retry_after": None,
                    }
            history["action"] = "retry_reconstruction_from_retained_measurements"
        else:
            # A failed run's images may have been released. New work keys reacquire full survey
            # coverage without deleting old scans, observations, windows or failed executions.
            nonce = uid()
            units = []
            for unit in doc["units"]:
                if unit["payload"].get("kind", "scan") != "scan":
                    continue
                payload = {**unit["payload"], "repair_run": nonce}
                job = enqueue_in_session(session, payload, batch.id)
                units.append(
                    {**unit, "payload": payload, "job_id": job.id, "disposition": "operator_restart"}
                )
            if not units:
                raise ValueError("Recording restart requires its original survey plan")
            clean.update({"phase": "survey", "phase_history": [], "repair_run": nonce, "units": units})
            history["action"] = "restart_and_reacquire_released_images"
        batch.document, batch.state = clean, "active"
        return {"id": batch.id, "state": batch.state, "action": history["action"]}


def pending_board_phase(session, root, batch, phase):
    doc, request = batch.document, batch.document["request"]
    current_frames = None
    if doc.get("repair_run"):
        acquisition_jobs = [
            u["job_id"] for u in doc["units"] if u["payload"].get("kind", "scan") in ("scan", "dense_window")
        ]
        current_frames = {
            sample["evidence_id"]
            for scan in session.scalars(select(Scan).where(Scan.job_id.in_(acquisition_jobs)))
            for sample in scan.document["samples"]
        }
        current_frames.update(
            eid
            for window in session.scalars(
                select(AnalysisWindow).where(AnalysisWindow.job_id.in_(acquisition_jobs))
            )
            for eid in window.document["anchor_evidence_ids"]
        )
    units, summary = ocr_units(
        session,
        root,
        AutomaticRequest(
            source_ids=request["source_ids"],
            end_seconds=request["end_seconds"],
            ocr_frames=64,
        ),
        doc["source_hashes"],
        allowed_evidence_ids={
            f.id
            for f in session.scalars(select(Evidence).where(Evidence.source_id.in_(request["source_ids"])))
            if f.document.get("storage_version") == STORAGE_VERSION
            and (current_frames is None or f.id in current_frames)
        },
        repair_run=doc.get("repair_run"),
    )
    if summary["matching_work_needs_attention"]:
        raise ValueError("Some board-reading jobs need attention; retry or finish their owning analysis")
    if units:
        append_phase(session, batch, phase, units, summary)
        return True
    return False


def detail_phase(session, root, batch):
    request = batch.document["request"]
    source_id = request["source_ids"][0]
    planned = adaptive_in_session(
        session,
        AdaptiveRequest(
            source_ids=[source_id],
            end_seconds=request["end_seconds"],
            max_jobs=request["detail_windows"],
            window_seconds=60,
            interval_seconds=1,
            max_image_growth_mb=request["max_image_growth_mb"],
            max_worker_seconds=request["max_worker_seconds"],
        ),
    )
    source, layout = source_or_error(session, source_id), profile()
    ending = {"windows": [], "deferred_windows": 0}
    candidates = planned["units"]
    displaced_candidates = 0
    if batch.document.get("ending_inspection_version"):
        from hogline.perception.broadcast import broadcast_catalog_in_session
        from hogline.reconstruction.scorecards import reconstruct_scorecards

        if batch.document["ending_inspection_version"] != ENDING_VERSION:
            raise ValueError("Ending inspection method changed; start a new recording analysis")
        observations = broadcast_catalog_in_session(session, root, source_id=source_id)["observations"]
        report = reconstruct_scorecards(source_id, observations)
        ending = ending_windows(report, request["end_seconds"], request["detail_windows"])
        priority = [
            {
                "payload": window_payload(
                    source,
                    WindowRequest(
                        source_id=source_id,
                        start_seconds=w["start_seconds"],
                        end_seconds=w["end_seconds"],
                        interval_seconds=1,
                        analyze_actors=False,
                    ),
                    layout,
                ),
                "selection": {**w, "method": ENDING_VERSION, "scorecard_sha256": report["sha256"]},
                "sample_count": math.ceil(w["end_seconds"] - w["start_seconds"]),
            }
            for w in ending["windows"]
        ]
        # Do not spend another window on an already-covered general activity sample.
        candidates = priority + [
            unit
            for unit in candidates
            if not any(
                w["start_seconds"] <= unit["payload"]["request"]["start_seconds"]
                and w["end_seconds"] >= unit["payload"]["request"]["end_seconds"]
                for w in ending["windows"]
            )
        ]
        displaced_candidates = max(0, len(candidates) - request["detail_windows"])
        candidates = candidates[: request["detail_windows"]]
    units = []
    for unit in candidates:
        # The recording pass prioritizes house/end evidence. Actor/pose work remains a focused tool.
        body = WindowRequest.model_validate(unit["payload"]["request"]).model_copy(
            update={"analyze_actors": False}
        )
        payload = window_payload(source, body, layout)
        if batch.document.get("repair_run"):
            payload["repair_run"] = batch.document["repair_run"]
        existing = session.scalar(select(Job).where(Job.job_key == digest(payload)))
        if existing and existing.state not in ("succeeded", "cancelled"):
            raise ValueError("A matching detailed window needs attention in another analysis")
        units.append(
            {
                **unit,
                "payload": payload,
                "disposition": "reuse_succeeded" if existing and existing.state == "succeeded" else "new",
            }
        )
    append_phase(
        session,
        batch,
        "detail",
        units,
        {
            "selected_windows": len(units),
            "deferred_candidates": planned["deferred_units"] + displaced_candidates,
            "selected_quiet_audits": sum(u["selection"].get("reason") == "quiet_audit" for u in units),
            "ending_inspection_windows": len(ending["windows"]),
            "deferred_ending_windows": ending["deferred_windows"],
        },
    )


def advance_recording_batch(session, root, batch):
    for _ in range(8):
        doc = batch.document
        if doc["recording_analysis_version"] != VERSION or doc["method_pin"] != method_pin():
            raise ValueError("Analysis methods changed; start a new recording analysis")
        for source_id, sha in doc["source_hashes"].items():
            if source_or_error(session, source_id)["metadata_sha256"] != sha:
                raise ValueError("Recording metadata changed; start a new analysis")
        if doc.get("active_execution") or any(
            session.get(Job, u["job_id"]).state != "succeeded" for u in doc["units"]
        ):
            return
        if doc["phase"] == "reconstruct":
            if not doc.get("analysis_result"):
                raise ValueError("The reconstruction result is missing")
            batch.state = "completed"
            status = doc.get("result_summary", {}).get("status", "proposals")
            batch.document = {**doc, "finished_at": now(), "analysis_outcome": f"finished_with_{status}"}
            freeze_outcomes(session, batch)
            from hogline.orchestration.auto_lineups import request_in_session

            # Face lineups follow every finished analysis (docs/LINEUPS.md, automatic lineups).
            request_in_session(session, doc["request"]["source_ids"][0], "recording_analysis")
            return
        limits = doc["limits"]
        if (
            doc["spent_worker_seconds"] >= limits["max_worker_seconds"]
            or doc["reserved_image_bytes"] >= limits["max_image_growth_mb"] * 1024**2
        ):
            raise ValueError("Analysis reached its shared time or storage limit; increase limits to continue")
        if doc["phase"] in ("survey", "boards"):
            if pending_board_phase(session, root, batch, "boards"):
                return
            detail_phase(session, root, batch)
        elif doc["phase"] in ("detail", "detail_boards"):
            if pending_board_phase(session, root, batch, "detail_boards"):
                return
            source_id = doc["request"]["source_ids"][0]
            payload = {
                "kind": "recording_reconstruction",
                "stage_version": VERSION,
                "source_hash": doc["source_hashes"][source_id],
                "analysis_id": batch.id,
                "request": {
                    "source_id": source_id,
                    "start_seconds": 0,
                    "end_seconds": doc["request"]["end_seconds"],
                    "max_worker_seconds": 120,
                    "max_evidence_mb": 1,
                },
            }
            append_phase(
                session,
                batch,
                "reconstruct",
                [{"payload": payload, "disposition": "new", "job_id": None, "sample_count": 1}],
                {},
            )
            return
        else:
            raise ValueError("Unknown recording-analysis stage")
    raise ValueError("Recording analysis exceeded its stage-advance bound")


def execute_reconstruction(db, job):
    started, token = time.monotonic(), job["lease_token"]
    check_execution(db, token, 0)
    result = source_end_timeline(db, job["request"]["source_id"])
    if len(json.dumps(result).encode()) > 4 * 1024**2:
        raise ValueError("Reconstruction exceeds the four-MiB report limit")
    with db.snapshot_session(write=True) as session:
        stopped = check_execution_in_session(session, token, time.monotonic() - started)
        if not stopped:
            batch = session.get(ProcessingBatch, job["analysis_id"])
            source = source_or_error(session, job["request"]["source_id"])
            if source["metadata_sha256"] != job["source_hash"]:
                raise ValueError("Recording metadata changed during reconstruction")
            batch.document = {
                **batch.document,
                "analysis_result": result,
                "result_sha256": digest(result),
                "result_summary": {
                    **{
                        k: result["result_review"][k]
                        for k in (
                            "status",
                            "candidate_games",
                            "known_end_scores",
                            "partial_score_claims",
                            "proposed_complete_games",
                        )
                    },
                    "observed_numbered_ends": result["result_review"]["known_end_scores"],
                    "acceptance": "proposed",
                },
            }
            row = session.get(Job, job["id"])
            row.state, row.lease_until = "succeeded", None
            row.document = {
                **row.document,
                "progress": 1,
                "finished_at": now(),
                "wall_seconds": time.monotonic() - started,
            }
            finish_execution(session, token, "succeeded", time.monotonic() - started)
    if stopped:
        from hogline.orchestration.plans import WorkStopped

        raise WorkStopped(stopped)
