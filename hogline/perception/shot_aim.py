"""Signed short-window delivery direction relative to a reviewed called broom line."""

import math

import numpy as np

from hogline.adapters.sources import digest
from hogline.perception.landmark_calibration import project_track

VERSION = "reviewed-post-release-line-v1"


def signed_line_angle(velocity, called, physical_right):
    v, c, right = (np.asarray(p, dtype=float) for p in (velocity, called, physical_right))
    cross_right = float(c[0] * right[1] - c[1] * right[0])
    if np.linalg.norm(c) < 1 or abs(cross_right) / np.linalg.norm(c) < 0.1:
        raise ValueError("Called line needs a distinct physical right-side marker")
    if np.linalg.norm(v) < 0.05 or float(v @ c) <= 0:
        raise ValueError("Path does not move toward the called end")
    cross = float(c[0] * v[1] - c[1] * v[0])
    return math.degrees(math.atan2(cross * math.copysign(1, cross_right), float(v @ c)))


def measure_aim(observation, calibration, fit, track, turn="unknown"):
    base = {
        "version": VERSION,
        "status": "unavailable",
        "line_error_degrees": None,
        "wide_error_degrees": None,
        "reference": "short_post_release_direction",
        "start_seconds": observation.start_seconds,
        "end_seconds": observation.end_seconds,
        "acceptance": observation.acceptance,
        "notice": "Fitted direction in a short reviewed post-release window, not a final-position error. "
        "Painted-plane handle projection does not correct height or lens distortion. "
        "The called broom line is not the intended resting point.",
    }
    if observation.calibration_id != calibration.id or observation.calibration_sha256 != digest(
        calibration.model_dump()
    ):
        return {**base, "reason": "camera_calibration_changed"}
    if track["id"] != observation.track_id:
        return {**base, "reason": "motion_track_changed"}
    if np.any(np.diff([p["source_seconds"] for p in track["points"]]) <= 0):
        return {**base, "reason": "track_timing_gap_or_reversal"}
    if not all(
        (
            observation.reviewed_free_travel,
            observation.reviewed_stone_association,
            observation.reviewed_called_line,
        )
    ):
        return {**base, "reason": "called_line_stone_or_free_travel_needs_review"}
    selected = [
        p
        for p in track["points"]
        if observation.start_seconds - 1e-6 <= p["source_seconds"] <= observation.end_seconds + 1e-6
    ]
    if len(selected) < 3 or selected[-1]["source_seconds"] - selected[0]["source_seconds"] < 0.15:
        return {**base, "reason": "insufficient_samples_in_aim_window"}
    if (
        selected[0]["source_seconds"] - observation.start_seconds > 0.15
        or observation.end_seconds - selected[-1]["source_seconds"] > 0.15
    ):
        return {**base, "reason": "aim_window_endpoint_not_observed"}
    segment = {
        **track,
        "points": selected,
        "start_seconds": selected[0]["source_seconds"],
        "end_seconds": selected[-1]["source_seconds"],
    }
    projected = project_track(segment, calibration, fit)
    if projected["status"] != "experimental_plane_measurement":
        return {**base, "reason": projected["reason"]}
    coordinates = np.array([p["plane_m"] for p in projected["points"]])
    if np.linalg.norm(coordinates[-1] - coordinates[0]) < 0.1:
        return {**base, "reason": "insufficient_observed_displacement"}
    called = np.array(observation.broom_m) - observation.line_origin_m
    right = np.array(observation.right_side_m) - observation.line_origin_m
    try:
        angle = signed_line_angle(projected["velocity_plane_mps"], called, right)
    except ValueError as exc:
        return {**base, "reason": str(exc)}
    if abs(angle) < 1e-10:
        angle = 0.0
    right_sign = math.copysign(1, called[0] * right[1] - called[1] * right[0])
    offsets = coordinates - observation.line_origin_m
    lateral = (called[0] * offsets[:, 1] - called[1] * offsets[:, 0]) * right_sign / np.linalg.norm(called)
    # Early/late fits expose curvature; this is a diagnostic, not an accuracy interval.
    headings = []
    if len(selected) >= 6:
        for indices in (range(len(selected) // 2), range(len(selected) // 2, len(selected))):
            indices = list(indices)
            times = np.array([selected[i]["source_seconds"] for i in indices])
            design = np.column_stack((times - times[0], np.ones(len(times))))
            velocity = np.linalg.lstsq(design, coordinates[indices], rcond=None)[0][0]
            try:
                headings.append(signed_line_angle(velocity, called, right))
            except ValueError:
                break
    return {
        **base,
        "status": "experimental_line_measurement",
        "line_error_degrees": angle,
        "wide_error_degrees": angle * (1 if turn == "left" else -1) if turn in ("left", "right") else None,
        "start_right_offset_m": float(lateral[0]),
        "end_right_offset_m": float(lateral[-1]),
        "sample_count": len(selected),
        "sample_span_seconds": [selected[0]["source_seconds"], selected[-1]["source_seconds"]],
        "plane_speed_mps": projected["speed_mps"],
        "fit_residual_rms_m": projected["fit_residual_rms_m"],
        "heading_change_degrees": (
            0.0 if abs(headings[1] - headings[0]) < 1e-10 else headings[1] - headings[0]
        )
        if len(headings) == 2
        else None,
        "maximum_two_pixel_sensitivity_m": max(p["two_pixel_sensitivity_m"] for p in projected["points"]),
        "weight_multiplier": None,
    }
