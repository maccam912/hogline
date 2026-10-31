"""Measured image trajectories with explicit painted-plane scale assumptions."""

import math

import numpy as np

from hogline.perception.perspective_stones import perspective_handles
from hogline.perception.projective_house import project_points
from hogline.perception.stones import RawHSVHandleDetector
from hogline.perception.tracking import track_handles

VERSION = "camera-motion-measurements-v5"
# World Curling R1(d), July 2024; a nominal prior, not a Wausau site survey.
NOMINAL_HOUSE_RADIUS_M = 1.829
DIMENSION_REFERENCE = "https://worldcurling.org/wp-content/uploads/2024/08/Rule-2024_Change-log_01082024.pdf"


def motion_detections(rgb, projection):
    """Retain raw candidates and apply a local colored-patch size prior, not object verification."""
    retained, rejected = [], []
    for index, candidate in enumerate(RawHSVHandleDetector().detect(rgb)):
        candidate = {**candidate, "raw_detection_index": index}
        if projection.get("status") == "candidate":
            x, y, w, h = candidate["bbox_px"]
            edges = project_points(
                [[x, y + h / 2], [x + w, y + h / 2], [x + w / 2, y], [x + w / 2, y + h]],
                projection["pixel_normalized_to_house"],
                projection["origin_px"],
                projection["scale_px"],
            )
            extent = max(np.linalg.norm(edges[1] - edges[0]), np.linalg.norm(edges[3] - edges[2]))
            candidate["colored_patch_extent_house_radii"] = float(extent)
            candidate["size_prior_basis"] = "painted_house_projection"
            # Local Wausau top-camera pilot prior; this is NOT the granite diameter.
            # Keeping the unit relative to the house makes the assumption explicit.
            if not 0.0875 <= extent <= 0.175:
                rejected.append({**candidate, "rejection_reason": "outside_local_colored_patch_size_prior"})
                continue
        else:
            extent = max(candidate["bbox_px"][2:]) / min(rgb.shape[:2])
            candidate["colored_patch_extent_crop_fraction"] = float(extent)
            candidate["size_prior_basis"] = "local_crop_fraction_without_physical_calibration"
            if not 0.039 <= extent <= 0.079:
                rejected.append({**candidate, "rejection_reason": "outside_local_crop_size_prior"})
                continue
        retained.append(candidate)
    return {"detections": retained, "rejected_candidates": rejected}


def motion_tracks(rows, input_sha256):
    return track_handles(
        rows,
        input_sha256,
        version=VERSION,
        views=("house_top", "house_bottom", "perspective_left", "perspective_right"),
        require_ring=False,
        min_samples=3,
        min_span_seconds=0.2,
        max_speed_fraction_per_second=1.2,
        view_speed_limits={"perspective_left": 0.35, "perspective_right": 0.35},
        prediction_error_fraction=0.02,
    )


def perspective_detections(rgb, people=()):
    """Reuse perspective granite-context and anonymous upper-body exclusion cues."""
    observation = perspective_handles(rgb, people)
    retained, rejected = [], []
    for index, detection in enumerate(observation["detections"]):
        item = {
            **detection,
            "raw_detection_index": index,
            "size_prior_basis": "perspective_granite_context_and_body_exclusion",
        }
        reasons = []
        if item["granite_context_fraction"] < 0.18:
            reasons.append("little_gray_context_around_handle")
        if item["person_overlap"]:
            reasons.append("color_inside_upper_person_box")
        if reasons:
            rejected.append({**item, "rejection_reasons": reasons})
        else:
            retained.append(item)
    return {
        "detections": retained,
        "rejected_candidates": rejected,
        "perspective_detection_status": observation["status"],
        "perspective_missing_reason": observation["missing_reason"],
    }


def release_phase(track, release_seconds):
    """Timing relative to a vision proposal, not proof of free travel or stone identity."""
    if release_seconds is None:
        return "release_unknown"
    if track["end_seconds"] < release_seconds:
        return "before_proposed_release"
    if track["start_seconds"] >= release_seconds:
        return "after_proposed_release"
    return "crosses_proposed_release"


def delivery_path_proposals(tracks, anchor, layout_id):
    """Rank review candidates with an explicit seed-camera flow prior; never identify a stone."""
    result = []
    for track in tracks:
        reasons = []
        supported = layout_id == "wausau-composite-seed-v1" and track["view"].startswith("perspective_")
        color = (anchor or {}).get("stone_color", "unknown")
        points = track["points"]
        dy = points[-1]["center_px"][1] - points[0]["center_px"][1]
        progress = dy / max(track["sampled_path_length_px"], 1e-8)
        if not supported:
            reasons.append("seed_camera_flow_not_applicable")
        if color == "unknown":
            reasons.append("delivery_color_unknown")
        elif color != track["color"]:
            reasons.append("different_from_proposed_delivery_color")
        if dy < max(8, track["crop_width"] * 0.02) or progress < 0.45:
            reasons.append("little_down_image_progress")
        result.append(
            {
                "track_id": track["id"],
                "status": "review_first"
                if not reasons
                else "other_image_fragment"
                if supported and color != "unknown"
                else "unavailable",
                "reasons": reasons,
                "down_image_displacement_px": dy,
                "down_image_progress_fraction": progress,
                "acceptance": "proposed",
                "basis": "Proposed delivery color and downward progress in the Wausau seed end camera. "
                "This heuristic is not calibrated confidence and does not prove free travel, "
                "stone identity or sweeping.",
            }
        )
    return result


def measure_track(track, projection, *, radius_m=NOMINAL_HOUSE_RADIUS_M):
    """Fit velocity over one observed fragment; never infer release, intent or sweeping correction."""
    if not math.isfinite(radius_m) or not 0.5 < radius_m < 3:
        raise ValueError("House radius must be a finite physical scale")
    points = track["points"]
    base = {
        "track_id": track["id"],
        "acceptance": "proposed",
        "view": track["view"],
        "color": track["color"],
        "point_count": len(points),
        "start_seconds": track["start_seconds"],
        "end_seconds": track["end_seconds"],
        "method": VERSION,
        "nominal_house_radius_m": radius_m,
        "dimension_reference": DIMENSION_REFERENCE,
        "weight_multiplier": None,
        "aim_error_degrees": None,
        "limitations": [
            "colored_handle_not_verified_granite_footprint",
            "painted_plane_projection_ignores_handle_height_and_lens_distortion",
            "nominal_ring_size_not_site_survey",
            "fragment_may_be_hand_controlled_or_swept",
            "reference_frame_geometry_not_independent_physical_validation",
        ],
    }
    if projection.get("status") != "candidate":
        return {**base, "status": "unavailable", "reason": "house_projection_unavailable"}
    times = np.array([p["source_seconds"] for p in points])
    if len(times) < 3 or np.any(np.diff(times) <= 0):
        return {**base, "status": "unavailable", "reason": "insufficient_ordered_samples"}
    coordinates = project_points(
        [p["center_px"] for p in points],
        projection["pixel_normalized_to_house"],
        projection["origin_px"],
        projection["scale_px"],
    )
    # Never extrapolate the painted-ring fit far beyond its observed support.
    if np.max(np.linalg.norm(coordinates, axis=1)) > 1.15:
        return {**base, "status": "unavailable", "reason": "outside_supported_house_region"}
    relative = times - times[0]
    design = np.column_stack((relative, np.ones(len(times))))
    fit = np.linalg.lstsq(design, coordinates, rcond=None)[0]
    residuals = coordinates - design @ fit
    speed = float(np.linalg.norm(fit[0]))
    displacement = float(np.linalg.norm(coordinates[-1] - coordinates[0]))
    if displacement < 0.04:
        return {**base, "status": "unavailable", "reason": "insufficient_observed_motion"}
    mapped = []
    for point, coordinate in zip(points, coordinates, strict=True):
        px = np.array(point["center_px"])
        probes = project_points(
            [px + [2, 0], px + [-2, 0], px + [0, 2], px + [0, -2]],
            projection["pixel_normalized_to_house"],
            projection["origin_px"],
            projection["scale_px"],
        )
        sensitivity = float(np.max(np.linalg.norm(probes - coordinate, axis=1))) * radius_m
        mapped.append(
            {
                **point,
                "house_coordinates": coordinate.tolist(),
                "nominal_plane_m": (coordinate * radius_m).tolist(),
                "two_pixel_sensitivity_m": sensitivity,
            }
        )
    return {
        **base,
        "status": "experimental_measurement",
        "points": mapped,
        "duration_seconds": float(relative[-1]),
        "nominal_plane_speed_mps": speed * radius_m,
        "speed_house_radii_per_second": speed,
        "nominal_plane_displacement_m": displacement * radius_m,
        "fit_residual_rms_m": float(np.sqrt(np.mean(np.sum(residuals**2, axis=1)))) * radius_m,
        "direction_house_coordinates": (fit[0] / speed).tolist() if speed > 0 else None,
        "sample_gaps_seconds": np.diff(times).tolist(),
        "notice": "Experimental painted-plane motion, not a release-weight or aim score.",
    }


def crossing_time(points, coordinate, value, *, max_gap_seconds=0.25):
    """Bracket a center crossing on observed neighboring points; never fill a blind section."""
    if coordinate not in (0, 1) or not math.isfinite(value) or max_gap_seconds <= 0:
        raise ValueError("Invalid crossing specification")
    crossings = []
    for a, b in zip(points, points[1:], strict=False):
        ta, tb = a["source_seconds"], b["source_seconds"]
        va, vb = a["nominal_plane_m"][coordinate], b["nominal_plane_m"][coordinate]
        if not 0 < tb - ta <= max_gap_seconds or va == vb or not min(va, vb) <= value < max(va, vb):
            continue
        fraction = (value - va) / (vb - va)
        crossings.append(
            {
                "seconds": ta + fraction * (tb - ta),
                "bracket_seconds": [ta, tb],
                "coordinate": coordinate,
                "value_m": value,
                "convention": "projected_handle_center",
                "acceptance": "proposed",
            }
        )
    return crossings
