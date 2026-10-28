"""Sampled house configurations and change brackets, without played-end claims."""

import math
from collections import Counter
from uuid import NAMESPACE_URL, uuid5

from hogline.perception.temporal import MAX_MOTION, match_configuration

VERSION = "sampled-house-states-v1"
MIN_SAMPLES = 4
MIN_SECONDS = 1.5
MAX_TRANSITION_SECONDS = 10


def ring_valid(row):
    ring = row.get("ring_reference") or {}
    return (
        ring.get("status") == "candidate"
        and all(
            isinstance(ring.get(key), (list, tuple))
            and len(ring[key]) == 2
            and all(isinstance(x, (int, float)) and math.isfinite(x) for x in ring[key])
            for key in ("center_px", "axes_px")
        )
        and all(x > 0 for x in ring["axes_px"])
        and isinstance(ring.get("angle_degrees"), (int, float))
        and math.isfinite(ring["angle_degrees"])
    )


def same_geometry(a, b):
    if not ring_valid(a) or not ring_valid(b):
        return False
    if (a["crop_width"], a["crop_height"]) != (b["crop_width"], b["crop_height"]):
        return False
    first, last = a["ring_reference"], b["ring_reference"]
    tolerance = max(2, min(a["crop_width"], a["crop_height"]) * 0.006)
    if math.dist(first["center_px"], last["center_px"]) > tolerance:
        return False
    if any(abs(x - y) > max(2, x * 0.02) for x, y in zip(first["axes_px"], last["axes_px"], strict=True)):
        return False
    # The major-axis angle is unstable for an almost circular ring.
    if max(first["axes_px"]) / min(first["axes_px"]) > 1.1:
        angle = abs((first["angle_degrees"] - last["angle_degrees"] + 90) % 180 - 90)
        if angle > 3:
            return False
    return True


def same_configuration(a, b):
    return (not a["detections"] and not b["detections"]) or match_configuration(
        a["detections"], b["detections"], a["crop_width"]
    )


def blockers(row):
    reasons = []
    if not ring_valid(row):
        reasons.append("missing_ring_geometry")
    if row.get("foreground_regions"):
        reasons.append("foreground_or_ring_obstruction")
    if any(d["boundary_clipped"] for d in row["detections"]):
        reasons.append("clipped_handle_candidate")
    if row.get("rejected_candidates"):
        reasons.append("unresolved_color_candidates")
    motion = row.get("motion_mean_abs")
    if motion is not None and (not math.isfinite(motion) or motion > MAX_MOTION):
        reasons.append("motion_or_exposure_change")
    return reasons


def contiguous(a, b):
    return (
        b["sample_index"] == a["sample_index"] + 1
        and 0 < b["source_seconds"] - a["source_seconds"] <= 1.5 * b["requested_interval_seconds"] + 0.05
    )


def anchor(row):
    return {
        key: row[key]
        for key in (
            "sample_index",
            "source_seconds",
            "pts",
            "time_base_num",
            "time_base_den",
            "decoded_rgb_sha256",
            "youtube_url",
        )
    }


def house_states(rows, input_sha256):
    states, transitions = [], []
    diagnostics = Counter()

    def identifier(kind, view, indices):
        return str(uuid5(NAMESPACE_URL, f"{VERSION}:{input_sha256}:{kind}:{view}:{indices}"))

    def finish(run):
        if not run:
            return
        distinct = len({r["decoded_rgb_sha256"] for r in run})
        if len(run) < MIN_SAMPLES or run[-1]["source_seconds"] - run[0]["source_seconds"] < MIN_SECONDS:
            diagnostics["short_runs_excluded"] += 1
            return
        if distinct < MIN_SAMPLES:
            diagnostics["repeated_image_runs_excluded"] += 1
            return
        first, last = run[0], run[-1]
        indices = [r["sample_index"] for r in run]
        states.append(
            {
                "id": identifier("state", first["view"], indices),
                "view": first["view"],
                "kind": "stable_handle_configuration"
                if first["detections"]
                else "stable_no_handle_detections",
                "start_seconds": first["source_seconds"],
                "end_seconds": last["source_seconds"],
                "sample_indices": indices,
                "distinct_composite_images": distinct,
                "candidate_count": len(first["detections"]),
                "color_counts": dict(Counter(d["color"] for d in first["detections"])),
                "representative_handles": first["detections"],
                "ring_reference": first["ring_reference"],
                "first": anchor(first),
                "last": anchor(last),
                "acceptance": "proposed",
                "score": None,
                "official_end_number": None,
            }
        )

    for view in ("house_top", "house_bottom"):
        ordered = sorted((r for r in rows if r["view"] == view), key=lambda r: r["sample_index"])
        run = []
        for row in ordered:
            reasons = blockers(row)
            diagnostics.update(reasons)
            if run and (
                reasons
                or not contiguous(run[-1], row)
                or not same_geometry(run[0], row)
                or not same_geometry(run[-1], row)
                or not same_configuration(run[0], row)
                or not same_configuration(run[-1], row)
            ):
                finish(run)
                run = []
            if not reasons:
                run.append(row)
        finish(run)
        view_states = [s for s in states if s["view"] == view]
        by_index = {r["sample_index"]: r for r in ordered}
        for before, after in zip(view_states, view_states[1:], strict=False):
            left, right = by_index[before["sample_indices"][-1]], by_index[after["sample_indices"][0]]
            if same_configuration(left, right):
                continue
            context = [
                r for r in ordered if left["sample_index"] <= r["sample_index"] <= right["sample_index"]
            ]
            if (
                right["source_seconds"] - left["source_seconds"] > MAX_TRANSITION_SECONDS
                or any(not contiguous(a, b) for a, b in zip(context, context[1:], strict=False))
                or any(not same_geometry(left, r) for r in context)
            ):
                diagnostics["unresolved_change_gaps"] += 1
                continue
            kind = (
                "possible_clearing"
                if before["candidate_count"] and not after["candidate_count"]
                else "handles_appear"
                if not before["candidate_count"]
                else "configuration_changed"
            )
            transitions.append(
                {
                    "id": identifier("change", view, [left["sample_index"], right["sample_index"]]),
                    "view": view,
                    "kind": kind,
                    "before_state_id": before["id"],
                    "after_state_id": after["id"],
                    "earliest_seconds": left["source_seconds"],
                    "latest_seconds": right["source_seconds"],
                    "before": anchor(left),
                    "after": anchor(right),
                    "intervening_flags": sorted({reason for r in context for reason in blockers(r)}),
                    "acceptance": "proposed",
                    "played_end_boundary": None,
                    "official_end_number": None,
                }
            )
    return {
        "version": VERSION,
        "minimum_samples": MIN_SAMPLES,
        "minimum_span_seconds": MIN_SECONDS,
        "maximum_transition_seconds": MAX_TRANSITION_SECONDS,
        "states": sorted(states, key=lambda s: (s["start_seconds"], s["view"])),
        "transitions": sorted(transitions, key=lambda t: (t["earliest_seconds"], t["view"])),
        "diagnostics": dict(sorted(diagnostics.items())),
        "notice": (
            "Stable means sampled image-space handle detections under experimental visibility checks. "
            "No detections does not establish an empty house. Changes may reflect detection errors, "
            "play, practice or cleanup; they do not establish a played end or score. Distinct composite "
            "images do not rule out a frozen individual camera panel. Physical A/B mapping is unresolved."
        ),
    }
