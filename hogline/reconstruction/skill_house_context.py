"""Sampled inner-house changes from retained survey images, never accepted ends."""

import math
from uuid import NAMESPACE_URL, uuid5

from hogline.adapters.sources import digest
from hogline.perception.house_score import normalized_point
from hogline.reconstruction.house_states import house_states, ring_valid, same_geometry

VERSION = "retained-survey-inner-house-v2"
CORE_RADIUS = 0.85
MAX_CLEARING_SECONDS = 180


def inner_house_rows(rows):
    output = []
    for row in rows:
        if not ring_valid(row):
            output.append(row)
            continue
        ring = row["ring_reference"]

        def inside(point, ring=ring):
            return math.hypot(*normalized_point(point, ring)) <= CORE_RADIUS

        regions = []
        for region in row["foreground_regions"]:
            x, y, w, h = region["bbox_px"]
            center = normalized_point([x + w / 2, y + h / 2], ring)
            corners = [normalized_point(p, ring) for p in ((x, y), (x + w, y), (x, y + h), (x + w, y + h))]
            if math.hypot(*center) - max(math.dist(center, p) for p in corners) <= CORE_RADIUS:
                regions.append(region)
        output.append(
            {
                **row,
                "detections": [d for d in row["detections"] if inside(d["center_px"])],
                "rejected_candidates": [d for d in row["rejected_candidates"] if inside(d["center_px"])],
                "foreground_regions": regions,
            }
        )
    return output


def coverage_intervals(rows):
    samples = sorted({r["source_seconds"]: r["requested_interval_seconds"] for r in rows}.items())
    intervals = []
    for seconds, interval in samples:
        if (
            not intervals
            or seconds - intervals[-1]["last_seconds"]
            > max(interval, intervals[-1]["last_interval"]) * 1.5 + 0.05
        ):
            intervals.append({"start_seconds": seconds, "last_seconds": seconds, "last_interval": interval})
        else:
            intervals[-1].update(last_seconds=seconds, last_interval=interval)
    return [{k: v for k, v in i.items() if k != "last_interval"} for i in intervals]


def infer_house_context(rows, input_key):
    core = inner_house_rows(rows)
    full = house_states(rows, digest({"version": VERSION, "input": input_key, "region": "full"}))
    result = house_states(core, digest({"version": VERSION, "input": input_key, "radius": CORE_RADIUS}))
    original = {(r["view"], r["sample_index"]): r for r in rows}
    coverage = coverage_intervals(rows)
    states = result["states"]
    for state in states:
        first = original[(state["view"], state["sample_indices"][0])]
        state.update(crop_width=first["crop_width"], crop_height=first["crop_height"])
        for endpoint in ("first", "last"):
            sample = original[(state["view"], state[endpoint]["sample_index"])]
            state[endpoint]["image_reference"] = sample["image_reference"]
    clearing, unresolved = [], []
    for view in ("house_top", "house_bottom"):
        occupied = None
        for state in sorted((s for s in states if s["view"] == view), key=lambda s: s["start_seconds"]):
            if state["candidate_count"] >= 3:
                occupied = state
                continue
            if state["candidate_count"] or state["end_seconds"] - state["start_seconds"] < 6:
                continue
            if occupied:
                start, end = occupied["end_seconds"], state["start_seconds"]
                reasons = []
                if not 0 < end - start <= MAX_CLEARING_SECONDS:
                    reasons.append("transition_interval_too_long")
                if not same_geometry(occupied, state):
                    reasons.append("ring_geometry_changed")
                if not any(i["start_seconds"] <= start and i["last_seconds"] >= end for i in coverage):
                    reasons.append("sample_coverage_gap")
                target = unresolved if reasons else clearing
                target.append(
                    {
                        "id": str(uuid5(NAMESPACE_URL, f"{VERSION}:{input_key}:{view}:{start}:{end}")),
                        "view": view,
                        "earliest_seconds": start,
                        "latest_seconds": end,
                        "before": occupied["last"],
                        "after": state["first"],
                        "before_candidate_count": occupied["candidate_count"],
                        "zero_detections_through_seconds": state["end_seconds"],
                        "acceptance": "proposed",
                        "official_end_number": None,
                        "kind": "unresolved_house_change" if reasons else "possible_inner_house_clearing",
                        "unresolved_reasons": reasons,
                        "notice": "May be play, cleanup, practice, occlusion or missed detections; "
                        "review the clip before assigning an end.",
                    }
                )
            occupied = None
    return {
        "version": VERSION,
        "region_outer_house_radii": CORE_RADIUS,
        "states": states,
        "possible_clearings": clearing,
        "unresolved_changes": unresolved,
        "coverage_intervals": coverage,
        "diagnostics": result["diagnostics"],
        "full_house_diagnostics": full["diagnostics"],
        "full_house_state_count": len(full["states"]),
        "sample_count": len({r["sample_index"] for r in rows}),
        "notice": "Downsampled retained-image detections inside 85% of the outer-house radius. "
        "Outside stones are omitted. No detections does not establish an empty house, "
        "completed end, score or throwing order.",
    }
