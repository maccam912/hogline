"""Conservative hand/control/separation brackets for candidate perspective releases."""

import math
from collections import Counter
from uuid import NAMESPACE_URL, uuid5

from hogline.adapters.sources import digest

VERSION = "perspective-release-associations-v2"


def visible_wrist(person, index):
    pose = person.get("body_pose") or {}
    if pose.get("status") != "observed":
        return None
    return next(
        (
            p
            for p in pose["landmarks"]
            if p["index"] == index and p["inside_crop"] and min(p["raw_visibility"], p["raw_presence"]) >= 0.7
        ),
        None,
    )


def release_associations(rows, actors, handles, input_sha256):
    by_row = {(r["view"], r["sample_index"]): r for r in rows}
    actor_at, person_at = {}, {}
    for actor in actors:
        for point in actor["points"]:
            index = point.get("person_index")
            if index is None:
                continue
            key = (actor["view"], point["sample_index"], index)
            actor_at[key] = actor["id"] if key not in actor_at else None
            person_at[(actor["id"], point["sample_index"])] = index
    candidates, uncertain, diagnostics = [], [], Counter()
    for track in handles:
        if track["motion_class"] != "position_change_observed":
            continue
        view, points = track["view"], track["points"]
        controls = []
        flags = set()
        for point in points:
            row = by_row[(view, point["sample_index"])]
            radius = max(5, max(point["candidate_bbox_px"][2:]) * 1.5)
            matches = []
            for index, person in enumerate(row["people"]):
                actor_id = actor_at.get((view, point["sample_index"], index))
                if actor_id is None:
                    continue
                for wrist_index in (15, 16):
                    wrist = visible_wrist(person, wrist_index)
                    if wrist:
                        distance = math.dist(point["center_px"], wrist["center_px"])
                        if distance <= radius:
                            matches.append((distance, actor_id, wrist_index))
            if len({actor for _, actor, _ in matches}) > 1:
                flags.add("competing_visible_hands")
                controls.append(None)
            elif matches:
                distance, actor_id, wrist_index = min(matches)
                controls.append({"actor_id": actor_id, "wrist_index": wrist_index, "distance_px": distance})
            else:
                controls.append(None)
        runs = []
        for i, control in enumerate(controls):
            if control is None:
                continue
            key = (control["actor_id"], control["wrist_index"])
            if runs and runs[-1]["key"] == key and runs[-1]["indices"][-1] == i - 1:
                runs[-1]["indices"].append(i)
            else:
                runs.append({"key": key, "indices": [i]})
        track_candidates = []
        for run in runs:
            indices = run["indices"]
            first, last = points[indices[0]], points[indices[-1]]
            if len(indices) < 3 or last["source_seconds"] - first["source_seconds"] < 0.5:
                continue
            prelude = []
            for index in indices:
                prelude.append(points[index])
                if prelude[-1]["source_seconds"] - prelude[0]["source_seconds"] >= 0.5:
                    break
            if prelude[-1]["source_seconds"] - prelude[0]["source_seconds"] < 0.5 or max(
                math.dist(prelude[0]["center_px"], p["center_px"]) for p in prelude
            ) > max(2, track["crop_width"] * 0.006):
                flags.add("no_observed_stationary_control_prelude")
                continue
            if math.dist(first["center_px"], last["center_px"]) < max(5, track["crop_width"] * 0.015):
                flags.add("no_coupled_motion_before_separation")
                continue
            actor_id, wrist_index = run["key"]
            separated = []
            for point in points[indices[-1] + 1 :]:
                if point["source_seconds"] - last["source_seconds"] > 2:
                    break
                row = by_row[(view, point["sample_index"])]
                person_index = person_at.get((actor_id, point["sample_index"]))
                wrist = (
                    visible_wrist(row["people"][person_index], wrist_index)
                    if person_index is not None
                    else None
                )
                radius = max(5, max(point["candidate_bbox_px"][2:]))
                if wrist is None or math.dist(wrist["center_px"], point["center_px"]) < radius * 3:
                    separated = []
                    continue
                separated.append(point)
                if (
                    len(separated) >= 3
                    and separated[-1]["source_seconds"] - separated[0]["source_seconds"] >= 0.5
                ):
                    break
            if len(separated) < 3 or separated[-1]["source_seconds"] - separated[0]["source_seconds"] < 0.5:
                flags.add("separation_or_actor_continuity_unobserved")
                continue
            if math.dist(last["center_px"], separated[-1]["center_px"]) < max(10, track["crop_width"] * 0.03):
                flags.add("object_did_not_continue_after_separation")
                continue
            after = separated[0]
            document = {
                "kind": "candidate_hand_to_object_release",
                "view": view,
                "color": track["color"],
                "handle_track_id": track["id"],
                "actor_track_id": actor_id,
                "wrist_landmark_index": wrist_index,
                "earliest_release_seconds": last["source_seconds"],
                "latest_release_seconds": after["source_seconds"],
                "before": last,
                "after": after,
                "control_sample_indices": [points[i]["sample_index"] for i in indices],
                "separated_sample_indices": [p["sample_index"] for p in separated],
                "acceptance": "proposed",
                "person_id": None,
                "played_shot_id": None,
                "weight_measurement": None,
                "unresolved": [
                    "object_class_not_independently_verified",
                    "sheet_and_end_phase_unresolved",
                    "named_actor_link_unavailable",
                    "calibrated_timing_endpoints_unavailable",
                    *sorted(flags),
                ],
            }
            document["id"] = str(
                uuid5(NAMESPACE_URL, digest({"version": VERSION, "input": input_sha256, **document}))
            )
            track_candidates.append(document)
        if len(track_candidates) > 1:
            for candidate in track_candidates:
                candidate["unresolved"].append("multiple_release_interpretations_in_fragment")
        candidates.extend(track_candidates)
        if not track_candidates:
            reasons = sorted(flags or {"no_observed_persistent_hand_control"})
            diagnostics.update(reasons)
            uncertain.append(
                {
                    "handle_track_id": track["id"],
                    "view": view,
                    "color": track["color"],
                    "start_seconds": track["start_seconds"],
                    "end_seconds": track["end_seconds"],
                    "reasons": reasons,
                    "control_intervals": [
                        {
                            "actor_track_id": run["key"][0],
                            "wrist_landmark_index": run["key"][1],
                            "first": points[run["indices"][0]],
                            "last": points[run["indices"][-1]],
                            "sample_count": len(run["indices"]),
                        }
                        for run in sorted(runs, key=lambda r: (-len(r["indices"]), r["indices"][0]))[:4]
                        if len(run["indices"]) >= 3
                        and points[run["indices"][-1]]["source_seconds"]
                        - points[run["indices"][0]]["source_seconds"]
                        >= 0.5
                    ],
                }
            )
    return {
        "version": VERSION,
        "candidates": candidates,
        "uncertain_motion": uncertain,
        "diagnostics": dict(diagnostics),
        "notice": (
            "Hand/control/separation proposals, not accepted deliveries. "
            "Object class, sheet/end phase, named actor and calibrated timing still need evidence. "
            "Sweeping or cleanup can resemble a release; no player weight statistic is inferred."
        ),
    }
