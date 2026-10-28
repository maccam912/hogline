"""Select bounded detailed windows from sparse observations, including quiet audits."""

import math
from collections import Counter

from pydantic import Field, model_validator
from sqlalchemy import select

from hogline.adapters.sources import digest, video_link
from hogline.config import profile
from hogline.db import Evidence, Job, Scan, now
from hogline.domain.catalog import Conflict, source_or_error
from hogline.domain.models import Model
from hogline.orchestration.plans import publish_batch
from hogline.orchestration.windows import WindowRequest, window_payload
from hogline.reconstruction.activity import VERSION as ACTIVITY_VERSION

VERSION = "activity-guided-window-selection-v1"


class AdaptiveRequest(Model):
    source_ids: list[str] = Field(min_length=1, max_length=16)
    start_seconds: float = Field(default=0, ge=0)
    end_seconds: float | None = Field(default=None, gt=0)
    window_seconds: int = Field(default=10, ge=4, le=60)
    interval_seconds: float = Field(default=0.5, ge=0.25, le=2)
    max_jobs: int = Field(default=8, ge=1, le=64)
    quiet_audit_fraction: float = Field(default=0.2, ge=0, le=0.5)
    selection_seed: str = Field(default="pilot-1", min_length=1, max_length=80)
    max_image_growth_mb: int = Field(default=128, ge=1, le=4096)
    max_worker_seconds: int = Field(default=600, ge=10, le=86400)
    job_evidence_mb: int = Field(default=32, ge=1, le=256)
    job_worker_seconds: int = Field(default=120, ge=10, le=600)

    @model_validator(mode="after")
    def bounded(self):
        if len(set(self.source_ids)) != len(self.source_ids):
            raise ValueError("Select each source once")
        if self.end_seconds is not None and self.end_seconds <= self.start_seconds:
            raise ValueError("Adaptive range end must follow start")
        return self


class AdaptiveStart(Model):
    request: AdaptiveRequest
    expected_plan_sha256: str = Field(min_length=64, max_length=64)


def overlaps(a, b):
    return a[0] < b[1] - 1e-6 and b[0] < a[1] - 1e-6


def adaptive_in_session(session, request):
    layout = profile()
    sources = {sid: source_or_error(session, sid) for sid in sorted(request.source_ids)}
    evidence = {
        e.id: e.document for e in session.scalars(select(Evidence).where(Evidence.source_id.in_(sources)))
    }
    jobs = list(session.scalars(select(Job)))
    by_key = {j.job_key: j for j in jobs}
    samples, exclusions = {}, Counter()
    # Only current, explicitly pinned scans can choose more media work.
    for scan, job in session.execute(
        select(Scan, Job).join(Job, Scan.job_id == Job.id).where(Scan.source_id.in_(sources))
    ):
        source, doc = sources[scan.source_id], scan.document
        if job.document.get("source_hash") != source["metadata_sha256"]:
            exclusions["stale_source_scan"] += 1
            continue
        if (
            doc.get("version") != ACTIVITY_VERSION
            or doc.get("profile", {}).get("views") != layout["views"]
            or digest(doc.get("profile", {})) != job.document.get("profile_sha256")
        ):
            exclusions["earlier_scan_method_or_layout"] += 1
            continue
        scan_request = doc["request"]
        start = scan_request.get("ownership_start_seconds")
        end = scan_request.get("ownership_end_seconds")
        start = scan_request["start_seconds"] if start is None else start
        end = scan_request["end_seconds"] if end is None else end
        for sample in doc["samples"]:
            seconds = sample["seconds"]
            if not sample.get("owned", start <= seconds < end):
                continue
            if not request.start_seconds <= seconds < (request.end_seconds or math.inf):
                continue
            if sample["evidence_id"] not in evidence:
                exclusions["missing_sample_evidence"] += 1
                continue
            key = (scan.source_id, round(sample.get("requested_seconds", seconds), 6))
            item = {
                **sample,
                "source_id": scan.source_id,
                "scan_id": scan.id,
                "scan_sha256": digest(doc),
                "interval": scan_request["interval_seconds"],
                "owner_start": start,
                "owner_end": end,
            }
            # Stable ownership dedup; a lone detail frame must not replace motion evidence.
            rank = (sample["state"] == "unknown", scan_request["interval_seconds"], scan.id)
            if key not in samples or rank < samples[key][0]:
                samples[key] = (rank, item)
            if len(samples) > 10000:
                raise ValueError("Adaptive preview exceeds 10,000 samples; narrow the range")
    candidates, previous = [], {}
    for _, sample in sorted(samples.values(), key=lambda entry: (entry[1]["source_id"], entry[1]["seconds"])):
        sid, seconds = sample["source_id"], sample["seconds"]
        source, state = sources[sid], sample["state"]
        limit = min(source.get("duration_seconds") or math.inf, request.end_seconds or math.inf)
        if not math.isfinite(limit):
            raise ValueError("An unknown-duration source needs an explicit adaptive range end")
        before = previous.get(sid)
        contiguous = (
            before
            and before["owner_end"] >= sample["owner_start"]
            and 0 < seconds - before["seconds"] <= 2 * max(sample["interval"], before["interval"])
        )
        transition = bool(
            contiguous and {state, before["state"]} == {"quiet_candidate", "activity_candidate"}
        )
        previous[sid] = sample
        reason = (
            "activity_transition"
            if transition
            else "motion_activity"
            if state == "activity_candidate"
            else "quiet_audit"
            if state == "quiet_candidate"
            else "uncertain_or_dark_sample"
        )
        start = max(
            request.start_seconds, min(seconds - request.window_seconds / 2, limit - request.window_seconds)
        )
        end = min(start + request.window_seconds, limit)
        if end - start < request.interval_seconds * 2:
            exclusions["insufficient_source_duration"] += 1
            continue
        unit_request = WindowRequest(
            source_id=sid,
            start_seconds=start,
            end_seconds=end,
            interval_seconds=request.interval_seconds,
            max_frames=240,
            max_evidence_mb=request.job_evidence_mb,
            max_worker_seconds=request.job_worker_seconds,
        )
        payload = window_payload(source, unit_request, layout)
        exact = by_key.get(digest(payload))
        # Reuse covering current work even if its original resource allowances differ.
        covering = [
            j
            for j in jobs
            if j.document.get("kind") == "dense_window"
            and all(
                j.document.get(k) == payload[k]
                for k in (
                    "source_hash",
                    "profile_sha256",
                    "stage_version",
                    "decoder_version",
                    "perception_version",
                )
            )
            and j.document["request"]["source_id"] == sid
            and j.document["request"]["start_seconds"] <= start
            and j.document["request"]["end_seconds"] >= end
            and j.document["request"]["interval_seconds"] <= request.interval_seconds
            and j.state in ("succeeded", "queued", "running")
        ]
        if covering:
            exclusions[
                "already_completed"
                if any(j.state == "succeeded" for j in covering)
                else "already_in_progress"
            ] += 1
            continue
        if exact and not (exact.state == "cancelled" and exact.attempts < 3):
            exclusions["matching_work_needs_attention"] += 1
            continue
        candidates.append(
            {
                "payload": payload,
                "job_id": exact.id if exact else None,
                "disposition": "resume_cancelled" if exact else "new",
                "sample_count": math.ceil((end - start) / request.interval_seconds),
                "selection": {
                    "reason": reason,
                    "source_id": sid,
                    "sample_seconds": seconds,
                    "evidence_ids": ([before["evidence_id"]] if transition else []) + [sample["evidence_id"]],
                    "supporting_samples": [
                        {k: item[k] for k in ("scan_id", "scan_sha256", "evidence_id", "seconds", "state")}
                        for item in ([before, sample] if transition else [sample])
                    ],
                    "scan_id": sample["scan_id"],
                    "scan_sha256": sample["scan_sha256"],
                    "source_metadata_sha256": source["metadata_sha256"],
                    "youtube_url": video_link(source, seconds),
                    "priority": {
                        "activity_transition": 0,
                        "motion_activity": 1,
                        "uncertain_or_dark_sample": 2,
                        "quiet_audit": 3,
                    }[reason],
                    "tie_break": digest([request.selection_seed, sid, seconds]),
                },
            }
        )
    units, selected_per_source = [], Counter()

    def admit(pool, count):
        for _ in range(count):
            eligible = [
                c
                for c in pool
                if c not in units
                and not any(
                    c["selection"]["source_id"] == u["selection"]["source_id"]
                    and overlaps(
                        (c["payload"]["request"]["start_seconds"], c["payload"]["request"]["end_seconds"]),
                        (u["payload"]["request"]["start_seconds"], u["payload"]["request"]["end_seconds"]),
                    )
                    for u in units
                )
            ]
            if not eligible:
                break
            chosen = min(
                eligible,
                key=lambda c: (
                    selected_per_source[c["selection"]["source_id"]],
                    c["selection"]["priority"],
                    c["selection"]["tie_break"],
                ),
            )
            units.append(chosen)
            selected_per_source[chosen["selection"]["source_id"]] += 1

    audit_slots = math.ceil(request.max_jobs * request.quiet_audit_fraction)
    admit([c for c in candidates if c["selection"]["reason"] == "quiet_audit"], audit_slots)
    admit(candidates, request.max_jobs - len(units))
    plan = {
        "kind": "adaptive_windows",
        "selection_version": VERSION,
        "request": request.model_dump(),
        "units": units,
        "total_requested_units": len(candidates),
        "admitted_new_units": len(units),
        "reused_succeeded_units": exclusions["already_completed"],
        "deferred_units": len(candidates) - len(units),
        "eligible_sparse_samples": len(samples),
        "exclusions": dict(exclusions),
        "requested_quiet_audit_slots": audit_slots,
        "selected_quiet_audits": sum(u["selection"]["reason"] == "quiet_audit" for u in units),
        "estimated_new_sample_requests": sum(u["sample_count"] for u in units),
        "image_growth_estimate_mb": len(units) * request.job_evidence_mb,
        "estimate_basis": "sum of per-window retained-data ceilings; shared batch budget can pause earlier",
        "coverage_kind": "selected_short_windows_not_full_source_coverage",
        "created_at": now(),
    }
    plan["sha256"] = digest({k: v for k, v in plan.items() if k != "created_at"})
    return plan


def preview_adaptive(db, request):
    with db.snapshot_session() as session:
        return adaptive_in_session(session, request)


def create_adaptive_batch(db, request, expected_plan_sha256=None):
    with db.snapshot_session(write=True) as session:
        plan = adaptive_in_session(session, request)
        if expected_plan_sha256 is not None and plan["sha256"] != expected_plan_sha256:
            raise Conflict("Activity, source metadata, or matching work changed. Preview again.")
        if not plan["units"]:
            raise ValueError("No new detailed windows selected; scan activity or inspect exclusions first")
        return publish_batch(session, plan)
