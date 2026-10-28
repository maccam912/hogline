"""Select small windows inside a posted-board uncertainty bracket."""

from pydantic import Field
from sqlalchemy import select

from hogline.adapters.sources import digest, video_link
from hogline.config import profile
from hogline.db import AnalysisWindow, FrameObservation, Job, now
from hogline.domain.catalog import Conflict, source_or_error
from hogline.domain.models import Model
from hogline.orchestration.automatic import AutomaticRequest, method_pin
from hogline.orchestration.plans import publish_batch
from hogline.orchestration.windows import WindowRequest, window_payload
from hogline.perception.broadcast import broadcast_catalog_in_session

VERSION = "largest-board-evidence-gap-selection-v2"


class RefinementRequest(Model):
    change_id: str = Field(min_length=1, max_length=80)
    max_windows: int = Field(default=3, ge=1, le=8)
    max_rounds: int = Field(default=3, ge=1, le=4)
    target_bracket_seconds: int = Field(default=60, ge=5, le=600)
    window_seconds: int = Field(default=10, ge=4, le=30)
    max_image_growth_mb: int = Field(default=96, ge=1, le=4096)
    max_worker_seconds: int = Field(default=300, ge=10, le=86400)


class RefinementStart(Model):
    request: RefinementRequest
    expected_plan_sha256: str = Field(min_length=64, max_length=64)


def subtract(intervals, start, end):
    result = []
    for left, right in intervals:
        if end <= left or start >= right:
            result.append((left, right))
        else:
            if left < start:
                result.append((left, start))
            if end < right:
                result.append((end, right))
    return result


def refinement_in_session(session, root, request):
    broadcast = broadcast_catalog_in_session(session, root)
    change = next((c for c in broadcast["board_timeline"]["changes"] if c["id"] == request.change_id), None)
    if change is None:
        raise Conflict("This board-change bracket changed or is unavailable. Choose a current change.")
    source = source_or_error(session, change["source_id"])
    if source["metadata_sha256"] != change["source_metadata_sha256"]:
        raise Conflict("The board readings use an earlier source manifest; analyze current evidence first")
    left, right = change["before"]["source_seconds"], change["after"]["source_seconds"]
    layout = profile()
    campaign = AutomaticRequest(
        source_ids=[source["id"]],
        start_seconds=left,
        end_seconds=right,
        detail_jobs=request.max_windows,
        window_seconds=request.window_seconds,
        ocr_frames=120,
        max_image_growth_mb=request.max_image_growth_mb,
        max_worker_seconds=request.max_worker_seconds,
    )
    exemplar = window_payload(
        source,
        WindowRequest(source_id=source["id"], start_seconds=left, end_seconds=min(left + 1, right)),
        layout,
    )
    pin_fields = (
        "source_hash",
        "profile_sha256",
        "stage_version",
        "decoder_version",
        "perception_version",
        "visibility_version",
        "retention_version",
    )
    intervals, units, excluded = [(left, right)], [], []
    observed = set(
        session.scalars(
            select(FrameObservation.evidence_id).where(FrameObservation.model_version == broadcast["version"])
        )
    )
    windows = {
        w.job_id: w.document
        for w in session.scalars(select(AnalysisWindow).where(AnalysisWindow.source_id == source["id"]))
    }
    # Existing native windows constrain the next choices. Their unread anchors can
    # be reused by OCR without another decode. Running windows are not stolen.
    jobs = list(session.scalars(select(Job)))
    compatible = [
        j
        for j in jobs
        if j.document.get("kind") == "dense_window"
        and j.document["request"]["source_id"] == source["id"]
        and all(j.document.get(k) == exemplar[k] for k in pin_fields)
        and j.state in ("succeeded", "queued", "running")
    ]
    reusable = []
    for job in sorted(compatible, key=lambda j: (j.document["request"]["start_seconds"], j.id)):
        r = job.document["request"]
        if r["end_seconds"] <= left or r["start_seconds"] >= right:
            continue
        intervals = subtract(intervals, r["start_seconds"], r["end_seconds"])
        excluded.append(
            {
                "job_id": job.id,
                "state": job.state,
                "start_seconds": r["start_seconds"],
                "end_seconds": r["end_seconds"],
            }
        )
        if (
            job.state == "succeeded"
            and job.id in windows
            and left <= r["start_seconds"] < r["end_seconds"] <= right
            and any(i not in observed for i in windows[job.id]["anchor_evidence_ids"])
        ):
            reusable.append(job)
    for job in reusable[: request.max_windows]:
        payload = window_payload(source, WindowRequest.model_validate(job.document["request"]), layout)
        units.append(
            {
                "payload": payload,
                "job_id": job.id,
                "disposition": "reuse_succeeded",
                "sample_count": 0,
                "selection": {
                    "reason": "read_pending_board_anchors",
                    "change_id": change["id"],
                    "youtube_url": video_link(source, payload["request"]["start_seconds"]),
                },
            }
        )
    by_key = {j.job_key: j for j in jobs}
    attempts = 0
    while len(units) < request.max_windows and attempts < 128:
        attempts += 1
        gaps = [(a, b) for a, b in intervals if b - a >= request.window_seconds]
        if not gaps:
            break
        a, b = min(gaps, key=lambda span: (-(span[1] - span[0]), span[0]))
        start = (a + b - request.window_seconds) / 2
        end = start + request.window_seconds
        intervals = subtract(intervals, start, end)
        payload = window_payload(
            source,
            WindowRequest(
                source_id=source["id"],
                start_seconds=start,
                end_seconds=end,
                interval_seconds=0.5,
                max_frames=120,
                max_evidence_mb=32,
                max_worker_seconds=120,
            ),
            layout,
        )
        old = by_key.get(digest(payload))
        if old and not (old.state == "cancelled" and old.attempts < 3):
            excluded.append(
                {"job_id": old.id, "state": old.state, "start_seconds": start, "end_seconds": end}
            )
            continue
        units.append(
            {
                "payload": payload,
                "job_id": old.id if old else None,
                "disposition": "resume_cancelled" if old else "new",
                "sample_count": request.window_seconds * 2,
                "selection": {
                    "reason": "board_change_uncertainty",
                    "change_id": change["id"],
                    "bracket_start_seconds": left,
                    "bracket_end_seconds": right,
                    "evidence_ids": [change["before"]["evidence_id"], change["after"]["evidence_id"]],
                    "youtube_url": video_link(source, start),
                },
            }
        )
    new_count = sum(u["disposition"] != "reuse_succeeded" for u in units)
    plan = {
        "kind": "automatic",
        "campaign_purpose": "board_change_refinement",
        "phase": "detail",
        "request": campaign.model_dump(),
        "refinement_request": request.model_dump(),
        "selection_version": VERSION,
        "refinement_round": 1,
        "maximum_total_windows": request.max_windows * request.max_rounds,
        "refinement_round_outcomes": [],
        "method_pin": method_pin(),
        "source_hashes": {source["id"]: source["metadata_sha256"]},
        "board_change": change,
        "units": units,
        "total_requested_units": request.max_windows,
        "admitted_new_units": new_count,
        "reused_succeeded_units": len(units) - new_count,
        "deferred_units": 0,
        "excluded_intervals": excluded,
        "candidate_windows_considered": attempts,
        "unselected_gap_intervals": [list(i) for i in intervals],
        "pending_reuse_windows_deferred": max(0, len(reusable) - len(units)),
        "estimated_new_sample_requests": sum(u["sample_count"] for u in units),
        "image_growth_estimate_mb": new_count * 32,
        "estimate_basis": "sum of new per-window retained-data ceilings; shared budget may pause earlier",
        "coverage_kind": "selected_windows_inside_board_change_uncertainty",
        "phase_history": [
            {
                "phase": "detail",
                "kind": "board_change_refinement",
                "round": 1,
                "at": now(),
                "selected_windows": len(units),
                "deferred_units": 0,
                "selected_quiet_audits": 0,
            }
        ],
        "created_at": now(),
        "notice": (
            "Inspect a few short gaps, then read only their retained frames. "
            "Later rounds inspect remaining wide gaps within the original source interval and shared budget. "
            "The result may narrow, split, or leave the bracket unchanged; "
            "it never timestamps end completion."
        ),
    }
    # Phase timestamps are descriptive, not part of the reviewed work identity.
    stable = {k: v for k, v in plan.items() if k not in ("created_at", "phase_history")}
    plan["sha256"] = digest(stable)
    return plan


def preview_refinement(db, request):
    with db.snapshot_session() as session:
        return refinement_in_session(session, db.settings.data_dir, request)


def create_refinement_batch(db, request, expected_plan_sha256=None):
    with db.snapshot_session(write=True) as session:
        plan = refinement_in_session(session, db.settings.data_dir, request)
        if expected_plan_sha256 is not None and expected_plan_sha256 != plan["sha256"]:
            raise Conflict("Board evidence or matching work changed; preview this refinement again")
        if not plan["units"]:
            raise ValueError(
                "No new windows fit this bracket; reduce the window length or inspect existing work"
            )
        result = publish_batch(session, plan)
        return result


def next_refinement(session, root, document):
    """Inspect the completed round before deciding whether more bounded work is useful."""
    request = RefinementRequest.model_validate(document["refinement_request"])
    original = document["board_change"]
    timeline = broadcast_catalog_in_session(session, root)["board_timeline"]
    left, right = original["before"]["source_seconds"], original["after"]["source_seconds"]
    matching = sorted(
        [
            c
            for c in timeline["changes"]
            if c["source_id"] == original["source_id"]
            and c["color"] == original["color"]
            and c["source_metadata_sha256"] == original["source_metadata_sha256"]
            and c["model_version"] == original["model_version"]
            and left - 1e-6 <= c["before"]["source_seconds"] < c["after"]["source_seconds"] <= right + 1e-6
        ],
        key=lambda c: (-c["bracket_seconds"], c["before"]["source_seconds"], c["id"]),
    )
    wide = [c for c in matching if c["bracket_seconds"] > request.target_bracket_seconds]
    outcome = {
        "round": document.get("refinement_round", 1),
        "at": now(),
        "target_bracket_seconds": request.target_bracket_seconds,
        "remaining_brackets": [
            {
                "change_id": c["id"],
                "color": c["color"],
                "start_seconds": c["before"]["source_seconds"],
                "end_seconds": c["after"]["source_seconds"],
                "bracket_seconds": c["bracket_seconds"],
            }
            for c in matching
        ],
        "remaining_wide_brackets": len(wide),
        "widest_bracket_seconds": max((c["bracket_seconds"] for c in matching), default=None),
    }
    if not matching:
        return None, {**outcome, "status": "no_current_matching_bracket"}
    if not wide:
        return None, {**outcome, "status": "target_reached"}
    if outcome["round"] >= request.max_rounds:
        return None, {**outcome, "status": "round_limit"}
    # A previous round may reveal several changes; tackle the widest remaining
    # bracket first. The original color/source interval remains the scope limit.
    for change in wide[:64]:
        planned = refinement_in_session(session, root, request.model_copy(update={"change_id": change["id"]}))
        if planned["units"]:
            return planned, {
                **outcome,
                "status": "continue",
                "next_change_id": change["id"],
                "next_plan_sha256": planned["sha256"],
            }
    return None, {**outcome, "status": "no_available_windows"}
