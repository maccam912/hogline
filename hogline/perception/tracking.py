"""Short image-space handle associations, never physical stone or shot identities."""

import math
from collections import Counter
from uuid import NAMESPACE_URL, uuid5

import numpy as np
from scipy.optimize import linear_sum_assignment

from hogline.adapters.sources import digest

VERSION = "conservative-handle-tracklets-v1"
MIN_SAMPLES = 3
MIN_SPAN_SECONDS = 1.0
MAX_CANDIDATES = 64


def track_handles(
    rows,
    input_sha256,
    *,
    views=("house_top", "house_bottom"),
    version=VERSION,
    require_ring=True,
    min_samples=MIN_SAMPLES,
    min_span_seconds=MIN_SPAN_SECONDS,
    max_speed_fraction_per_second=0.35,
    view_speed_limits=None,
    prediction_error_fraction=None,
):
    if min_samples < 3 or min_span_seconds <= 0 or not 0 < max_speed_fraction_per_second <= 2:
        raise ValueError("Invalid tracking sample/span/speed limits")
    if view_speed_limits and any(
        view not in views or not 0 < speed <= 2 for view, speed in view_speed_limits.items()
    ):
        raise ValueError("Invalid per-view tracking speed limits")
    if prediction_error_fraction is not None and not 0 < prediction_error_fraction <= 0.1:
        raise ValueError("Invalid prediction error limit")
    tracks, diagnostics = [], Counter()
    for view in views:
        speed_limit = (view_speed_limits or {}).get(view, max_speed_fraction_per_second)
        active, previous = [], None

        def finish(track, reason, view=view):
            points = track["points"]
            if (
                len(points) < min_samples
                or points[-1]["source_seconds"] - points[0]["source_seconds"] < min_span_seconds
            ):
                diagnostics["short_fragments_excluded"] += 1
                return
            distance = math.dist(points[0]["center_px"], points[-1]["center_px"])
            length = sum(
                math.dist(a["center_px"], b["center_px"]) for a, b in zip(points, points[1:], strict=False)
            )
            document = {
                "kind": "image_space_handle_tracklet",
                "version": version,
                "view": view,
                "color": track["color"],
                "crop_width": track["width"],
                "crop_height": track["height"],
                "start_seconds": points[0]["source_seconds"],
                "end_seconds": points[-1]["source_seconds"],
                "points": points,
                "start_reason": track["start_reason"],
                "termination_reason": reason,
                "net_displacement_px": distance,
                "sampled_path_length_px": length,
                "motion_class": "position_change_observed"
                if distance >= max(4, track["width"] * 0.02)
                else "small_net_displacement",
                "acceptance": "proposed",
                "physical_stone_id": None,
                "shot_id": None,
                "speed_meters_per_second": None,
                "score": None,
            }
            document["id"] = str(uuid5(NAMESPACE_URL, digest({"input": input_sha256, **document})))
            tracks.append(document)

        def point(row, index, detection):
            result = {
                **{
                    k: row[k]
                    for k in (
                        "sample_index",
                        "source_seconds",
                        "pts",
                        "time_base_num",
                        "time_base_den",
                        "decoded_rgb_sha256",
                        "youtube_url",
                    )
                },
                "detection_index": index,
                "center_px": list(detection["center_px"]),
            }
            if "raw_detection_index" in detection:
                result["raw_detection_index"] = detection["raw_detection_index"]
                result["candidate_bbox_px"] = detection["bbox_px"]
            return result

        for row in sorted((r for r in rows if r["view"] == view), key=lambda r: r["source_seconds"]):
            diagnostics["sampled_views"] += 1
            dt = row["source_seconds"] - previous["source_seconds"] if previous else None
            break_reason = None
            if require_ring and row.get("ring_reference", {}).get("status") != "candidate":
                break_reason = "house_reference_unavailable"
            elif row.get("foreground_regions"):
                break_reason = "foreground_or_ring_obstruction"
            elif len(row["detections"]) > MAX_CANDIDATES:
                break_reason = "too_many_candidates"
            elif previous and (row["crop_width"], row["crop_height"]) != (
                previous["crop_width"],
                previous["crop_height"],
            ):
                break_reason = "crop_geometry_changed"
            elif previous and not 0 < dt <= 1.5 * row["requested_interval_seconds"] + 0.05:
                break_reason = "sample_gap"
            if break_reason:
                for track in active:
                    finish(track, break_reason)
                active = []
                diagnostics[break_reason] += 1
            previous = row
            if break_reason in (
                "house_reference_unavailable",
                "foreground_or_ring_obstruction",
                "too_many_candidates",
            ):
                continue
            detections = [(i, d) for i, d in enumerate(row["detections"]) if not d["boundary_clipped"]]
            diagnostics["clipped_candidates_excluded"] += len(row["detections"]) - len(detections)
            # Nearby same-color candidates cannot safely carry identity through a contact.
            crowded = {
                i
                for i, (_, a) in enumerate(detections)
                if any(
                    i != j
                    and a["color"] == b["color"]
                    and math.dist(a["center_px"], b["center_px"]) < max(3, row["crop_width"] * 0.025)
                    for j, (_, b) in enumerate(detections)
                )
            }
            gate = max(
                4,
                row["crop_width"] * min(0.4, speed_limit * (dt or row["requested_interval_seconds"])),
            )
            margin = max(2, row["crop_width"] * 0.015)
            costs = np.full((len(active), len(detections)), 1e9)
            predictions = []
            prediction_gates = []
            for a, track in enumerate(active):
                last = track["points"][-1]
                predicted = np.asarray(last["center_px"], dtype=float)
                if len(track["points"]) >= 2:
                    earlier = track["points"][-2]
                    predicted += (
                        (predicted - earlier["center_px"])
                        * dt
                        / (last["source_seconds"] - earlier["source_seconds"])
                    )
                prediction_gate = (
                    min(gate, max(3, row["crop_width"] * prediction_error_fraction))
                    if prediction_error_fraction is not None and len(track["points"]) >= 2
                    else gate
                )
                predictions.append(predicted)
                prediction_gates.append(prediction_gate)
                for b, (_, detection) in enumerate(detections):
                    if (
                        b not in crowded
                        and detection["color"] == track["color"]
                        and math.dist(last["center_px"], detection["center_px"]) <= gate
                    ):
                        distance = math.dist(predicted, detection["center_px"])
                        if distance <= prediction_gate:
                            costs[a, b] = distance
            ambiguous_tracks = {
                a
                for a, track in enumerate(active)
                if any(
                    track["color"] == detections[b][1]["color"]
                    and math.dist(
                        predictions[a]
                        if prediction_error_fraction is not None
                        else track["points"][-1]["center_px"],
                        detections[b][1]["center_px"],
                    )
                    <= prediction_gates[a]
                    for b in crowded
                )
            }
            ambiguous_detections = set(crowded)
            for a, values in enumerate(costs):
                finite = sorted((cost, b) for b, cost in enumerate(values) if cost < 1e9)
                if len(finite) > 1 and finite[1][0] - finite[0][0] <= margin:
                    ambiguous_tracks.add(a)
                    ambiguous_detections.update(b for cost, b in finite if cost - finite[0][0] <= margin)
            for b in range(len(detections)):
                finite = sorted((cost, a) for a, cost in enumerate(costs[:, b]) if cost < 1e9)
                if len(finite) > 1 and finite[1][0] - finite[0][0] <= margin:
                    ambiguous_detections.add(b)
                    ambiguous_tracks.update(a for cost, a in finite if cost - finite[0][0] <= margin)
            for a in ambiguous_tracks:
                costs[a, :] = 1e9
            for b in ambiguous_detections:
                costs[:, b] = 1e9
            matches = {}
            if active and detections:
                # Dummy assignments permit unmatched tracks without stealing feasible pairs.
                augmented = np.full((len(active), len(detections) + len(active)), 1.0)
                augmented[:, : len(detections)] = np.where(costs < 1e9, costs / gate / (len(active) + 1), 1e9)
                rr, cc = linear_sum_assignment(augmented)
                matches = {
                    int(a): int(b)
                    for a, b in zip(rr, cc, strict=True)
                    if b < len(detections) and costs[a, b] < 1e9
                }
            following = []
            for a, track in enumerate(active):
                if a in matches:
                    index, detection = detections[matches[a]]
                    track["points"].append(point(row, index, detection))
                    following.append(track)
                else:
                    finish(
                        track,
                        "ambiguous_association" if a in ambiguous_tracks else "no_matching_detection",
                    )
            used = set(matches.values())
            for b, (index, detection) in enumerate(detections):
                if b in used or b in ambiguous_detections:
                    continue
                following.append(
                    {
                        "color": detection["color"],
                        "width": row["crop_width"],
                        "height": row["crop_height"],
                        "points": [point(row, index, detection)],
                        "start_reason": break_reason or "first_unmatched_detection",
                    }
                )
            diagnostics["ambiguous_candidates_excluded"] += len(ambiguous_detections)
            active = following
        for track in active:
            finish(track, "window_boundary")
    return {
        "version": version,
        "tracks": sorted(tracks, key=lambda t: (t["start_seconds"], t["view"], t["id"])),
        "diagnostics": dict(diagnostics),
        "minimum_samples": min_samples,
        "minimum_span_seconds": min_span_seconds,
        "maximum_speed_fraction_per_second": max_speed_fraction_per_second,
        "view_speed_limits": view_speed_limits,
        "prediction_error_fraction": prediction_error_fraction,
        "association_gate": "same color, bounded displacement and velocity prediction; ambiguous pairs split",
        "notice": (
            "Discrete image-space handle fragments. No interpolation across missing/obstructed samples, "
            "cross-view fusion, physical identity, delivery event, metric speed, or score."
        ),
    }
