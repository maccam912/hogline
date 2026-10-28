"""Anonymous per-view body fragments; no named, roster or cross-camera identity."""

import math
from collections import Counter
from uuid import NAMESPACE_URL, uuid5

import numpy as np
from scipy.optimize import linear_sum_assignment

from hogline.adapters.sources import digest

VERSION = "perspective-actor-fragments-v2"


def actor_point(row, detection):
    return {
        **{
            k: row[k]
            for k in (
                "sample_index",
                "source_seconds",
                "pts",
                "time_base_num",
                "time_base_den",
                "youtube_url",
            )
        },
        "person_index": row["people"].index(detection),
        "bbox_px": detection["bbox_px"],
        "raw_model_score": detection["raw_model_score"],
        "body_pose_status": (detection.get("body_pose") or {}).get("status", "unavailable"),
    }


def foot(box):
    x, y, w, h = box
    return [x + w / 2, y + h]


def box_iou(a, b):
    overlap = max(0, min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0])) * max(
        0, min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1])
    )
    return overlap / max(1, a[2] * a[3] + b[2] * b[3] - overlap)


def actor_fragments(rows, input_sha256):
    tracks, diagnostics = [], Counter()
    for view in ("perspective_left", "perspective_right"):
        active, previous = [], None

        def finish(track, reason, view=view):
            points = track["points"]
            if len(points) < 3 or points[-1]["source_seconds"] - points[0]["source_seconds"] < 1:
                diagnostics["short_fragments_excluded"] += 1
                return
            displacement = math.dist(foot(points[0]["bbox_px"]), foot(points[-1]["bbox_px"]))
            document = {
                "view": view,
                "start_seconds": points[0]["source_seconds"],
                "end_seconds": points[-1]["source_seconds"],
                "points": points,
                "crop_width": track["width"],
                "crop_height": track["height"],
                "net_foot_displacement_px": displacement,
                "motion_class": "view_position_change_observed"
                if displacement >= max(4, track["height"] * 0.03)
                else "small_view_position_change",
                "low_posture_candidate_samples": sum(
                    p["bbox_px"][2] / p["bbox_px"][3] >= 0.75 for p in points
                ),
                "termination_reason": reason,
                "acceptance": "proposed",
                "person_id": None,
                "game_actor_id": None,
                "delivery_id": None,
                "sheet_membership": "unresolved",
            }
            document["id"] = str(uuid5(NAMESPACE_URL, digest({"input": input_sha256, **document})))
            tracks.append(document)

        for row in sorted((r for r in rows if r["view"] == view), key=lambda r: r["sample_index"]):
            diagnostics["sampled_views"] += 1
            gap = previous and (
                row["sample_index"] != previous["sample_index"] + 1
                or not 0
                < row["source_seconds"] - previous["source_seconds"]
                <= 1.5 * row["requested_interval_seconds"] + 0.05
                or (row["crop_width"], row["crop_height"])
                != (previous["crop_width"], previous["crop_height"])
            )
            if gap:
                for track in active:
                    finish(track, "sample_gap_or_geometry_change")
                active = []
                diagnostics["sample_gaps"] += 1
            previous = row
            detections = [d for d in row["people"] if not d["boundary_clipped"] and d["torso_hsv_histogram"]]
            diagnostics["clipped_or_tiny_people_excluded"] += len(row["people"]) - len(detections)
            costs = np.full((len(active), len(detections)), 1e9)
            for i, track in enumerate(active):
                last = track["points"][-1]
                for j, detection in enumerate(detections):
                    distance = (
                        math.dist(foot(last["bbox_px"]), foot(detection["bbox_px"])) / row["crop_height"]
                    )
                    appearance = float(
                        np.abs(np.asarray(track["appearance"]) - detection["torso_hsv_histogram"]).sum()
                    )
                    scale = max(last["bbox_px"][3], detection["bbox_px"][3]) / min(
                        last["bbox_px"][3], detection["bbox_px"][3]
                    )
                    dt = row["source_seconds"] - last["source_seconds"]
                    if distance <= min(0.2, 0.12 * dt) and appearance <= 0.65 and scale <= 1.8:
                        costs[i, j] = distance / max(0.001, min(0.2, 0.12 * dt)) + appearance
            crowded = {
                i
                for i, a in enumerate(detections)
                if any(
                    i != j
                    and box_iou(a["bbox_px"], b["bbox_px"]) > 0.5
                    and np.abs(np.asarray(a["torso_hsv_histogram"]) - b["torso_hsv_histogram"]).sum() <= 0.65
                    for j, b in enumerate(detections)
                )
            }
            ambiguous_columns = set(crowded)
            ambiguous_rows = {i for i in range(len(active)) if any(costs[i, j] < 1e9 for j in crowded)}
            for i, values in enumerate(costs):
                ranked = sorted((v, j) for j, v in enumerate(values) if v < 1e9)
                if len(ranked) > 1 and ranked[1][0] - ranked[0][0] <= 0.15:
                    ambiguous_rows.add(i)
                    ambiguous_columns.update(j for v, j in ranked if v - ranked[0][0] <= 0.15)
            for j in range(len(detections)):
                ranked = sorted((v, i) for i, v in enumerate(costs[:, j]) if v < 1e9)
                if len(ranked) > 1 and ranked[1][0] - ranked[0][0] <= 0.15:
                    ambiguous_columns.add(j)
                    ambiguous_rows.update(i for v, i in ranked if v - ranked[0][0] <= 0.15)
            for i in ambiguous_rows:
                costs[i, :] = 1e9
            for j in ambiguous_columns:
                costs[:, j] = 1e9
            matches = {}
            if active and detections:
                augmented = np.ones((len(active), len(detections) + len(active)))
                augmented[:, : len(detections)] = np.where(costs < 1e9, costs / (3 * (len(active) + 1)), 1e9)
                rr, cc = linear_sum_assignment(augmented)
                matches = {
                    int(i): int(j)
                    for i, j in zip(rr, cc, strict=True)
                    if j < len(detections) and costs[i, j] < 1e9
                }
            following = []
            for i, track in enumerate(active):
                if i not in matches:
                    finish(track, "ambiguous_association" if i in ambiguous_rows else "no_matching_person")
                else:
                    detection = detections[matches[i]]
                    track["points"].append(actor_point(row, detection))
                    following.append(track)
            used = set(matches.values()) | ambiguous_columns
            for j, detection in enumerate(detections):
                if j in used:
                    continue
                following.append(
                    {
                        "appearance": detection["torso_hsv_histogram"],
                        "width": row["crop_width"],
                        "height": row["crop_height"],
                        "points": [actor_point(row, detection)],
                    }
                )
            diagnostics["ambiguous_people_excluded"] += len(ambiguous_columns)
            active = following
        for track in active:
            finish(track, "window_boundary")
    return {
        "version": VERSION,
        "tracks": sorted(tracks, key=lambda t: (t["start_seconds"], t["view"], t["id"])),
        "diagnostics": dict(diagnostics),
        "notice": (
            "Anonymous per-view person fragments based on body position and torso clothing color. "
            "Ambiguous associations split. Adjacent-sheet people and spectators may be included. "
            "A low posture is only a cue; no thrower, delivery, named identity "
            "or weight measurement is established."
        ),
    }
