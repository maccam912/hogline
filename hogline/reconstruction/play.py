"""House-focused sampled play cues, retaining uncertainty about throws and end phase."""

import math
from collections import Counter

from hogline.perception.house_score import normalized_point
from hogline.reconstruction.house_states import house_states, ring_valid

VERSION = "house-focused-play-cues-v1"
HOUSE_MARGIN = 1.16


def focused_play(rows, input_sha256):
    """Ignore parked objects outside a stated ellipse ROI, never hidden objects inside it."""
    filtered, diagnostics = [], Counter()
    for row in rows:
        if not ring_valid(row):
            filtered.append(row)
            continue
        ring = row["ring_reference"]

        def inside(point, ring=ring):
            return math.hypot(*normalized_point(point, ring)) <= HOUSE_MARGIN

        detections = [d for d in row["detections"] if inside(d["center_px"])]
        rejected = [d for d in row.get("rejected_candidates", []) if inside(d["center_px"])]
        regions = []
        for region in row.get("foreground_regions", []):
            x, y, w, h = region["bbox_px"]
            # Conservative rectangle/circle test in normalized ellipse coordinates:
            # include any region whose circumscribed normalized circle reaches the ROI.
            center = normalized_point([x + w / 2, y + h / 2], ring)
            corners = [normalized_point(p, ring) for p in ((x, y), (x + w, y), (x, y + h), (x + w, y + h))]
            radius = max(math.dist(center, c) for c in corners)
            if math.hypot(*center) - radius <= HOUSE_MARGIN:
                regions.append(region)
        diagnostics["outside_house_detections_excluded"] += len(row["detections"]) - len(detections)
        diagnostics["outside_house_rejections_excluded"] += len(row.get("rejected_candidates", [])) - len(
            rejected
        )
        filtered.append(
            {**row, "detections": detections, "rejected_candidates": rejected, "foreground_regions": regions}
        )
    result = house_states(filtered, f"{VERSION}:{input_sha256}")
    by_index = {(r["view"], r["sample_index"]): r for r in filtered}
    for state in result["states"]:
        first = by_index[(state["view"], state["sample_indices"][0])]
        state["crop_width"], state["crop_height"] = first["crop_width"], first["crop_height"]
    arrivals, settling = [], []
    for change in result["transitions"]:
        if change["kind"] == "handles_appear":
            arrivals.append(
                {
                    "id": change["id"],
                    "view": change["view"],
                    "before": change["before"],
                    "after": change["after"],
                    "kind": "first_visible_house_handles_candidate",
                    "first_throw": None,
                    "official_end_number": None,
                    "reason": "could_be_arrival_placement_or_detection_recovery",
                }
            )
        elif change["kind"] == "configuration_changed":
            after = next(s for s in result["states"] if s["id"] == change["after_state_id"])
            indices = range(change["before"]["sample_index"] + 1, change["after"]["sample_index"])
            motion = [
                by_index[(change["view"], i)]
                for i in indices
                if (change["view"], i) in by_index and by_index[(change["view"], i)].get("motion_mean_abs")
            ]
            if motion:
                settling.append(
                    {
                        "view": change["view"],
                        "kind": "configuration_settled_candidate",
                        "last_changed_observation": change["before"],
                        "stable_from": after["first"],
                        "stable_through": after["last"],
                        "last_stone_of_end": None,
                        "physical_shot_id": None,
                    }
                )
    return {
        **result,
        "version": VERSION,
        "region_outer_house_radii": HOUSE_MARGIN,
        "arrival_candidates": arrivals,
        "settling_candidates": settling,
        "diagnostics": dict(Counter(result["diagnostics"]) + diagnostics),
        "notice": (
            "Sampled handle changes inside a provisional outer-house ellipse plus 16% margin. "
            "Outside parked objects are excluded; guards outside this region are unobserved. "
            "Arrivals are not counted throws; stable samples do not establish the last stone "
            "of an end. Clearing and first-handle appearances remain candidates."
        ),
    }


def bracket_play_changes(results):
    """Join stable occupied/zero anchors across up to three minutes of sampled obstruction.

    Every intermediate source sample must belong to contiguous dense coverage with
    matching acquisition/crop/detector/visibility provenance. No activity is filled in.
    """
    from hogline.adapters.sources import digest
    from hogline.reconstruction.house_states import same_geometry

    groups = {}
    for result in results:
        if result.get("version") != VERSION or "dense_coverage" not in result:
            continue
        key = tuple(
            result[k]
            for k in (
                "source_id",
                "source_metadata_sha256",
                "profile_sha256",
                "detector_version",
                "visibility_version",
            )
        )
        groups.setdefault(key, []).append(result)
    output = []
    for group in groups.values():
        for view in ("house_top", "house_bottom"):
            states = sorted(
                ((state, r) for r in group for state in r["states"] if state["view"] == view),
                key=lambda pair: (pair[0]["start_seconds"], pair[0]["end_seconds"], pair[0]["id"]),
            )
            for (before, left), (after, _right) in zip(states, states[1:], strict=False):
                start, end = before["end_seconds"], after["start_seconds"]
                if (
                    bool(before["candidate_count"]) == bool(after["candidate_count"])
                    or not 0 < end - start <= 180
                    or not same_geometry(before, after)
                ):
                    continue
                coverage = sorted(
                    (
                        r["dense_coverage"]["start_seconds"],
                        r["dense_coverage"]["last_sample_seconds"] + r["dense_coverage"]["interval_seconds"],
                        r["window_id"],
                        r["sha256"],
                    )
                    for r in group
                )
                cursor, supporting = start, []
                for lo, hi, window_id, sha in coverage:
                    if hi <= cursor or lo > cursor + 0.001:
                        continue
                    cursor = hi
                    supporting.append((window_id, sha))
                    if cursor >= end:
                        break
                if cursor < end:
                    continue
                change = {
                    "id": digest([VERSION, before["id"], after["id"], supporting]),
                    "view": view,
                    "kind": "possible_clearing" if before["candidate_count"] else "handles_appear",
                    "before_state_id": before["id"],
                    "after_state_id": after["id"],
                    "earliest_seconds": start,
                    "latest_seconds": end,
                    "before": before["last"],
                    "after": after["first"],
                    "intervening_flags": ["activity_between_stable_anchors_unresolved"],
                    "acceptance": "proposed",
                    "played_end_boundary": None,
                    "official_end_number": None,
                }
                document = {
                    **{
                        k: left[k]
                        for k in (
                            "source_id",
                            "source_metadata_sha256",
                            "profile_sha256",
                            "window_id",
                            "version",
                            "status",
                        )
                    },
                    "id": digest(["clearing-bracket", change]),
                    "supporting_window_ids": [s[0] for s in supporting],
                    "input_result_refs": supporting,
                    "states": [before, after],
                    "transitions": [change],
                    "arrival_candidates": (
                        [
                            {
                                "id": change["id"],
                                "view": view,
                                "before": before["last"],
                                "after": after["first"],
                                "kind": "first_visible_house_handles_candidate",
                                "first_throw": None,
                                "official_end_number": None,
                                "reason": "continuous_sampled_coverage_but_intervening_activity_unresolved",
                            }
                        ]
                        if not before["candidate_count"]
                        else []
                    ),
                }
                output.append({"sha256": digest(document), **document})
    return output
