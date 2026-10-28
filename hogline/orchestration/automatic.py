"""A finite scan → detail → OCR campaign sharing one durable batch budget."""

from pydantic import Field, model_validator
from sqlalchemy import select

from hogline.adapters.sources import digest, video_link
from hogline.config import profile
from hogline.db import AnalysisWindow, Evidence, FrameObservation, Job, ProcessingBatch, now
from hogline.domain.catalog import Conflict, source_or_error
from hogline.domain.models import Model
from hogline.orchestration.adaptive import AdaptiveRequest, adaptive_in_session
from hogline.orchestration.plans import (
    PlanRequest,
    enqueue_in_session,
    freeze_outcomes,
    plan_in_session,
    publish_batch,
)
from hogline.orchestration.windows import WindowRequest, window_payload
from hogline.perception.broadcast import VERSION as OCR_VERSION
from hogline.perception.broadcast import broadcast_context
from hogline.reconstruction.activity import VERSION as SCAN_VERSION

VERSION = "bounded-automatic-campaign-v1"


class AutomaticRequest(Model):
    source_ids: list[str] = Field(min_length=1, max_length=16)
    start_seconds: float = Field(default=0, ge=0)
    end_seconds: float = Field(gt=0)
    scan_jobs: int = Field(default=2, ge=1, le=32)
    scan_interval_seconds: int = Field(default=30, ge=5, le=60)
    detail_jobs: int = Field(default=4, ge=1, le=32)
    window_seconds: int = Field(default=10, ge=4, le=60)
    ocr_frames: int = Field(default=24, ge=1, le=120)
    max_image_growth_mb: int = Field(default=128, ge=1, le=4096)
    max_worker_seconds: int = Field(default=600, ge=10, le=86400)

    @model_validator(mode="after")
    def bounded(self):
        if len(set(self.source_ids)) != len(self.source_ids):
            raise ValueError("Select each source once")
        if self.end_seconds <= self.start_seconds:
            raise ValueError("Campaign end must follow start")
        if 600 % self.scan_interval_seconds:
            raise ValueError("Scan interval must divide a ten-minute chunk")
        return self


class AutomaticStart(Model):
    request: AutomaticRequest
    expected_plan_sha256: str = Field(min_length=64, max_length=64)


def method_pin():
    # Pin every method that can affect the automatic choices before any acquisition.
    layout = profile()
    sample = window_payload(
        {"metadata_sha256": "pin"},
        WindowRequest(source_id="pin", start_seconds=0, end_seconds=10),
        layout,
    )
    return {
        "campaign": VERSION,
        "scan": SCAN_VERSION,
        "ocr": OCR_VERSION,
        **{
            k: sample[k]
            for k in (
                "profile_sha256",
                "stage_version",
                "decoder_version",
                "perception_version",
                "visibility_version",
                "retention_version",
            )
        },
    }


def automatic_in_session(session, request):
    scan = PlanRequest(
        source_ids=request.source_ids,
        start_seconds=request.start_seconds,
        end_seconds=request.end_seconds,
        chunk_seconds=600,
        context_seconds=0,
        interval_seconds=request.scan_interval_seconds,
        max_jobs=request.scan_jobs,
        acquisition_tier="coarse",
        max_image_growth_mb=request.max_image_growth_mb,
        max_worker_seconds=request.max_worker_seconds,
        job_evidence_mb=64,
        job_worker_seconds=300,
    )
    parts = [
        plan_in_session(session, scan.model_copy(update={"source_ids": [sid]})) for sid in request.source_ids
    ]
    reused = [u for part in parts for u in part["units"] if u["disposition"] == "reuse_succeeded"]
    pending = [[u for u in part["units"] if u["disposition"] != "reuse_succeeded"] for part in parts]
    fair = [group[i] for i in range(max(map(len, pending), default=0)) for group in pending if i < len(group)]
    selected = fair[: request.scan_jobs]
    plan = {
        **parts[0],
        "units": [*reused, *selected],
        "total_requested_units": sum(p["total_requested_units"] for p in parts),
        "admitted_new_units": len(selected),
        "reused_succeeded_units": len(reused),
        "deferred_units": sum(p["total_requested_units"] for p in parts) - len(reused) - len(selected),
        "estimated_new_sample_requests": sum(u["sample_count"] for u in selected),
        "image_growth_estimate_mb": round(sum(u["sample_count"] for u in selected) * 0.2, 2),
    }
    plan.update(
        {
            "kind": "automatic",
            "phase": "scan",
            "request": request.model_dump(),
            "method_pin": method_pin(),
            "source_hashes": {
                sid: source_or_error(session, sid)["metadata_sha256"] for sid in request.source_ids
            },
            "phase_history": [],
            "coverage_kind": "bounded_sparse_scan_selected_windows_and_retained_frame_ocr",
            "notice": (
                "Later windows are selected after the scan, including quiet audits. "
                "All stages share one budget. No game or result is accepted automatically."
            ),
        }
    )
    plan["sha256"] = digest({k: v for k, v in plan.items() if k not in ("created_at", "sha256")})
    return plan


def preview_automatic(db, request):
    with db.snapshot_session() as session:
        return automatic_in_session(session, request)


def create_automatic_batch(db, request, expected_plan_sha256=None):
    with db.snapshot_session(write=True) as session:
        plan = automatic_in_session(session, request)
        if expected_plan_sha256 is not None and plan["sha256"] != expected_plan_sha256:
            raise Conflict("Sources or matching scan work changed; preview the campaign again")
        return publish_batch(session, plan)


def ocr_units(session, root, request, hashes, allowed_evidence_ids=None):
    layout = profile()
    context = broadcast_context(session, root, layout)
    version = context["version"]
    processed = set(
        session.scalars(select(FrameObservation.evidence_id).where(FrameObservation.model_version == version))
    )
    frames = []
    for frame in session.scalars(select(Evidence).where(Evidence.source_id.in_(request.source_ids))):
        doc = frame.document
        if (
            (allowed_evidence_ids is None or frame.id in allowed_evidence_ids)
            and frame.id not in processed
            and doc.get("acquisition_tier") != "coarse"
            and doc.get("source_metadata_sha256") == hashes[frame.source_id]
            and request.start_seconds <= doc["source_seconds"] < request.end_seconds
        ):
            frames.append(frame)
    # Round robin by recording, then chronological: a long source cannot monopolize OCR.
    per_source = {}
    for frame in sorted(frames, key=lambda f: (f.source_id, f.document["source_seconds"], f.id)):
        per_source.setdefault(frame.source_id, []).append(frame)
    ordered = [
        group[i]
        for i in range(max(map(len, per_source.values()), default=0))
        for group in per_source.values()
        if i < len(group)
    ]
    units, busy = [], 0
    existing = {j.job_key: j for j in session.scalars(select(Job))}
    for frame in ordered:
        seconds = frame.document["source_seconds"]
        payload = {
            "kind": "broadcast_ocr",
            "stage_version": version,
            "profile": layout,
            "profile_sha256": digest(layout),
            "source_hash": hashes[frame.source_id],
            "evidence_id": frame.id,
            "evidence_sha256": digest(frame.document),
            "request": {
                "source_id": frame.source_id,
                "start_seconds": seconds,
                "end_seconds": seconds,
                "max_worker_seconds": 60,
                "max_evidence_mb": 8,
            },
        }
        old = existing.get(digest(payload))
        if old and not (old.state == "cancelled" and old.attempts < 3):
            busy += 1
            continue
        if len(units) < request.ocr_frames:
            units.append(
                {
                    "payload": payload,
                    "job_id": old.id if old else None,
                    "disposition": "resume_cancelled" if old else "new",
                    "sample_count": 1,
                    "selection": {
                        "reason": "retained_frame_ocr",
                        "evidence_ids": [frame.id],
                        "youtube_url": video_link(source_or_error(session, frame.source_id), seconds),
                    },
                }
            )
    return units, {
        "pending_eligible_frames": len(ordered),
        "selected_frames": len(units),
        "matching_work_needs_attention": busy,
        "deferred_frames": len(ordered) - len(units),
        "model_version": version,
    }


def advance_automatic(db):
    """Called by the worker before claiming. Stage publication is atomic and restart safe."""
    with db.snapshot_session(write=True) as session:
        for batch in session.scalars(select(ProcessingBatch).where(ProcessingBatch.state == "active")):
            doc = batch.document
            if doc.get("kind") != "automatic" or doc.get("active_execution"):
                continue
            if any(session.get(Job, u["job_id"]).state != "succeeded" for u in doc["units"]):
                continue
            try:
                if doc["method_pin"] != method_pin():
                    raise ValueError(
                        "Automatic processing methods changed; cancel and preview a new campaign"
                    )
                for sid, sha in doc["source_hashes"].items():
                    if source_or_error(session, sid)["metadata_sha256"] != sha:
                        raise ValueError("Source metadata changed; cancel and preview a new campaign")
                limits = doc.get("limits", doc["request"])
                if (
                    doc["spent_worker_seconds"] >= limits["max_worker_seconds"]
                    or doc["reserved_image_bytes"] >= limits["max_image_growth_mb"] * 1024**2
                ):
                    raise ValueError("Shared campaign budget reached before the next stage")
                if doc.get("campaign_purpose") == "board_change_refinement":
                    from hogline.orchestration.board_refinement import VERSION as REFINEMENT_VERSION

                    if doc["selection_version"] != REFINEMENT_VERSION:
                        raise ValueError("Board refinement method changed; preview a current refinement")
                request = AutomaticRequest.model_validate(doc["request"])
                # At most two transitions; empty stages do not need another poll.
                while batch.document["phase"] != "ocr":
                    doc = batch.document
                    if doc["phase"] == "scan":
                        planned = adaptive_in_session(
                            session,
                            AdaptiveRequest(
                                source_ids=request.source_ids,
                                start_seconds=request.start_seconds,
                                end_seconds=request.end_seconds,
                                max_jobs=request.detail_jobs,
                                window_seconds=request.window_seconds,
                                quiet_audit_fraction=0.2,
                                max_image_growth_mb=request.max_image_growth_mb,
                                max_worker_seconds=request.max_worker_seconds,
                            ),
                        )
                        phase, units = "detail", planned["units"]
                        summary = {
                            k: planned[k]
                            for k in (
                                "sha256",
                                "eligible_sparse_samples",
                                "deferred_units",
                                "exclusions",
                                "selected_quiet_audits",
                            )
                        }
                    else:
                        phase = "ocr"
                        allowed = None
                        if doc.get("campaign_purpose") == "board_change_refinement":
                            job_ids = [u["job_id"] for u in doc["units"]]
                            allowed = {
                                i
                                for w in session.scalars(
                                    select(AnalysisWindow).where(AnalysisWindow.job_id.in_(job_ids))
                                )
                                for i in w.document["anchor_evidence_ids"]
                            }
                        units, summary = ocr_units(
                            session, db.settings.data_dir, request, doc["source_hashes"], allowed
                        )
                    added = []
                    for unit in units:
                        job = enqueue_in_session(session, unit["payload"], batch.id)
                        added.append({**unit, "job_id": job.id})
                    batch.document = {
                        **doc,
                        "phase": phase,
                        "units": [*doc["units"], *added],
                        "phase_history": [
                            *doc["phase_history"],
                            {"phase": phase, "at": now(), "job_ids": [u["job_id"] for u in added], **summary},
                        ],
                    }
                    if added:
                        break
                if (
                    all(session.get(Job, u["job_id"]).state == "succeeded" for u in batch.document["units"])
                    and batch.document["phase"] == "ocr"
                ):
                    batch.state = "completed"
                    freeze_outcomes(session, batch)
            except ValueError as exc:
                batch.state = "paused"
                batch.document = {**batch.document, "pause_reason": str(exc)}
        session.flush()
