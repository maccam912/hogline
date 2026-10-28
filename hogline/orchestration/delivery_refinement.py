"""Select short native-cadence windows only around supported release/control brackets."""

import math
from collections import Counter

from sqlalchemy import select

from hogline.adapters.sources import digest, video_link
from hogline.analytics.actors import checked_actor_result
from hogline.analytics.deliveries import checked_delivery_result
from hogline.db import AnalysisWindow, Job, WindowActors, WindowDeliveries
from hogline.domain.catalog import source_or_error
from hogline.orchestration.windows import WindowRequest, window_payload
from hogline.perception.actors import VERSION as ACTOR_VERSION
from hogline.reconstruction.deliveries import VERSION as DELIVERY_VERSION

VERSION = "bounded-delivery-bracket-refinement-v1"
MAX_BRACKETS_PER_RESULT = 8


def refinement_units(session, request, source_hashes, window_ids):
    diagnostics, proposed = Counter(), []
    if len(window_ids) > 32:
        raise ValueError("Delivery refinement accepts at most 32 parent windows")
    if request.delivery_refinement_jobs == 0 or not window_ids:
        return [], {"selection_version": VERSION, "selected_windows": 0, "diagnostics": {}}
    for row in session.scalars(
        select(WindowDeliveries)
        .where(
            WindowDeliveries.window_id.in_(window_ids),
            WindowDeliveries.version == DELIVERY_VERSION,
        )
        .order_by(WindowDeliveries.id)
    ):
        result = checked_delivery_result(row)
        window = session.get(AnalysisWindow, row.window_id)
        if (
            window is None
            or result["window_id"] != window.id
            or result["source_id"] != window.source_id
            or result["source_metadata_sha256"] != window.document["source_metadata_sha256"]
            or result["input_perspective"] != window.document["actor_input"]
        ):
            raise ValueError("Delivery refinement input disagrees with its source window")
        parent = session.get(WindowActors, result["actor_result_id"])
        if parent is None or checked_actor_result(parent)["sha256"] != result["actor_result_sha256"]:
            raise ValueError("Delivery refinement actor lineage is missing or corrupt")
        if parent.version != ACTOR_VERSION:
            diagnostics["earlier_actor_method_excluded"] += 1
            continue
        sid = result["source_id"]
        if sid not in source_hashes or result["source_metadata_sha256"] != source_hashes[sid]:
            diagnostics["earlier_source_manifest_excluded"] += 1
            continue
        brackets = [
            (c["earliest_release_seconds"], c["latest_release_seconds"], "release_bracket", 0)
            for c in result["candidates"][:MAX_BRACKETS_PER_RESULT]
        ]
        diagnostics["brackets_deferred_by_input_cap"] += max(
            0, len(result["candidates"]) - MAX_BRACKETS_PER_RESULT
        )
        for uncertain in result["uncertain_motion"]:
            if not set(uncertain["reasons"]) & {
                "no_observed_stationary_control_prelude",
                "separation_or_actor_continuity_unobserved",
            }:
                diagnostics["motion_without_supported_control_excluded"] += 1
                continue
            for control in uncertain.get("control_intervals", []):
                if control["sample_count"] >= 3:
                    a, b = control["first"]["source_seconds"], control["last"]["source_seconds"]
                    if b - a >= 0.5 and len(brackets) >= MAX_BRACKETS_PER_RESULT:
                        diagnostics["brackets_deferred_by_input_cap"] += 1
                    elif b - a >= 0.5:
                        brackets.append((b, b, "incomplete_control_separation", 1))
        source = source_or_error(session, sid)
        if source["metadata_sha256"] != source_hashes[sid]:
            raise ValueError("Delivery refinement source metadata changed")
        for first, last, reason, priority in brackets:
            if (
                not math.isfinite(first)
                or not math.isfinite(last)
                or first > last
                or not window.document["observed_first_seconds"]
                <= first
                <= last
                <= window.document["observed_last_seconds"]
            ):
                raise ValueError("Delivery refinement bracket is outside its original observations")
            center = (first + last) / 2
            lower, upper = (
                request.start_seconds,
                min(request.end_seconds, source.get("duration_seconds") or request.end_seconds),
            )
            if not lower <= center < upper:
                diagnostics["bracket_outside_campaign_excluded"] += 1
                continue
            start = max(lower, center - 2)
            end = min(upper, start + 4)
            start = max(lower, end - 4)
            if end - start < 1.5:
                diagnostics["insufficient_context_inside_campaign"] += 1
                continue
            planned = WindowRequest(
                source_id=sid,
                start_seconds=start,
                end_seconds=end,
                interval_seconds=1 / 30,
                max_frames=121,
                max_evidence_mb=32,
                max_worker_seconds=120,
            )
            payload = window_payload(source, planned)
            proposed.append(
                {
                    "payload": payload,
                    "sample_count": math.ceil((end - start) * 30),
                    "priority": priority,
                    "selection": {
                        "reason": reason,
                        "delivery_result_id": result["id"],
                        "delivery_result_sha256": result["sha256"],
                        "source_id": sid,
                        "center_seconds": center,
                        "youtube_url": video_link(source, center),
                    },
                }
            )
    ordered = sorted(
        proposed,
        key=lambda u: (u["priority"], u["selection"]["center_seconds"], u["selection"]["delivery_result_id"]),
    )
    # Recording round-robin prevents one source or duplicate camera views monopolizing the allowance.
    per_source = {}
    for unit in ordered:
        per_source.setdefault(unit["selection"]["source_id"], []).append(unit)
    fair = [
        group[i]
        for i in range(max(map(len, per_source.values()), default=0))
        for _, group in sorted(per_source.items())
        if i < len(group)
    ]
    selected = []
    for unit in fair:
        q = unit["payload"]["request"]
        if any(
            old["payload"]["request"]["source_id"] == q["source_id"]
            and min(old["payload"]["request"]["end_seconds"], q["end_seconds"])
            > max(old["payload"]["request"]["start_seconds"], q["start_seconds"])
            for old in selected
        ):
            diagnostics["overlapping_refinement_excluded"] += 1
            continue
        if len(selected) >= request.delivery_refinement_jobs:
            diagnostics["brackets_deferred_by_job_limit"] += 1
            continue
        old = session.scalar(select(Job).where(Job.job_key == digest(unit["payload"])))
        if old and old.state not in ("succeeded", "queued"):
            diagnostics["matching_refinement_needs_attention"] += 1
            continue
        selected.append(
            {k: v for k, v in unit.items() if k != "priority"}
            | {
                "job_id": old.id if old else None,
                "disposition": f"reuse_{old.state}" if old else "new",
            }
        )
    return selected, {
        "selection_version": VERSION,
        "eligible_brackets": len(proposed),
        "selected_windows": len(selected),
        "diagnostics": dict(diagnostics),
        "notice": (
            "Native-frame refinement shares the campaign budget; no shot, identity or weight is accepted."
        ),
    }
