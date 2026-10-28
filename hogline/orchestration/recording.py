"""A recording-wide, resumable survey → board → detail → reconstruction pipeline."""

import json
import math
import time

from pydantic import Field
from sqlalchemy import select

from hogline.adapters.sources import digest
from hogline.analytics.end_timeline import source_end_timeline
from hogline.config import profile
from hogline.db import Job, ProcessingBatch, now
from hogline.domain.catalog import source_or_error
from hogline.domain.models import Model
from hogline.orchestration.adaptive import AdaptiveRequest, adaptive_in_session
from hogline.orchestration.automatic import AutomaticRequest, append_phase, method_pin, ocr_units
from hogline.orchestration.plans import (
    PlanRequest,
    check_execution,
    check_execution_in_session,
    finish_execution,
    freeze_outcomes,
    plan_in_session,
    publish_batch,
)
from hogline.orchestration.windows import WindowRequest, window_payload

VERSION = "whole-recording-analysis-v1"
PHASES = ["survey", "boards", "detail", "detail_boards", "reconstruct"]


class RecordingAnalysisRequest(Model):
    max_worker_seconds: int | None = Field(default=None, ge=60, le=86400)
    max_image_growth_mb: int | None = Field(default=None, ge=32, le=4096)
    detail_windows: int | None = Field(default=None, ge=1, le=32)


def recording_plan(session, source_id, request):
    source = source_or_error(session, source_id)
    duration = source.get("duration_seconds")
    if not duration or not math.isfinite(duration) or duration <= 0:
        raise ValueError("Complete the recording metadata before starting full-recording analysis")
    if duration > 86400:
        raise ValueError("This pilot analyzes recordings up to 24 hours; split longer recordings first")
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
    if plan["deferred_units"]:
        raise ValueError("The full recording did not fit the survey plan")
    return {
        **plan,
        "kind": "automatic",
        "campaign_purpose": "recording_analysis",
        "recording_analysis_version": VERSION,
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
            select(ProcessingBatch).where(ProcessingBatch.state.in_(["active", "paused"]))
        ):
            if batch.document.get("campaign_purpose") == "recording_analysis" and batch.document["request"][
                "source_ids"
            ] == [source_id]:
                from hogline.orchestration.plans import batch_view

                return {**batch_view(session, batch), "already_exists": True}
        plan = recording_plan(session, source_id, request)
        plan["sha256"] = digest({k: v for k, v in plan.items() if k not in ("sha256", "created_at")})
        return {**publish_batch(session, plan), "already_exists": False}


def pending_board_phase(session, root, batch, phase):
    doc, request = batch.document, batch.document["request"]
    units, summary = ocr_units(
        session,
        root,
        AutomaticRequest(
            source_ids=request["source_ids"],
            end_seconds=request["end_seconds"],
            ocr_frames=64,
        ),
        doc["source_hashes"],
    )
    if summary["matching_work_needs_attention"]:
        raise ValueError("Some board-reading jobs need attention; retry or finish their owning analysis")
    if units:
        append_phase(session, batch, phase, units, summary)
        return True
    return False


def detail_phase(session, batch):
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
    units = []
    for unit in planned["units"]:
        # The recording pass prioritizes house/end evidence. Actor/pose work remains a focused tool.
        body = WindowRequest.model_validate(unit["payload"]["request"]).model_copy(
            update={"analyze_actors": False}
        )
        payload = window_payload(source, body, layout)
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
            "deferred_candidates": planned["deferred_units"],
            "selected_quiet_audits": planned["selected_quiet_audits"],
        },
    )


def advance_recording_batch(session, root, batch):
    for _ in range(8):
        doc = batch.document
        if doc.get("active_execution") or any(
            session.get(Job, u["job_id"]).state != "succeeded" for u in doc["units"]
        ):
            return
        if doc["recording_analysis_version"] != VERSION or doc["method_pin"] != method_pin():
            raise ValueError("Analysis methods changed; start a new recording analysis")
        for source_id, sha in doc["source_hashes"].items():
            if source_or_error(session, source_id)["metadata_sha256"] != sha:
                raise ValueError("Recording metadata changed; start a new analysis")
        if doc["phase"] == "reconstruct":
            if not doc.get("analysis_result"):
                raise ValueError("The reconstruction result is missing")
            batch.state = "completed"
            batch.document = {**doc, "finished_at": now(), "analysis_outcome": "finished_with_proposals"}
            freeze_outcomes(session, batch)
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
            detail_phase(session, batch)
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
                    "candidate_games": sum(bool(book["ends"]) for book in result["board_ledgers"]),
                    "observed_numbered_ends": sum(
                        end["scoring_color"] is not None
                        for book in result["board_ledgers"]
                        for end in book["ends"]
                    ),
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
