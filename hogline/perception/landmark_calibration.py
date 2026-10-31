"""Fit and independently check a planar camera map; prohibit unsupported extrapolation."""

import cv2
import numpy as np

VERSION = "reviewed-plane-landmarks-v1"


def map_points(points, matrix):
    xy = np.asarray(points, dtype=float)
    h = np.asarray(matrix, dtype=float)
    if (
        xy.ndim != 2
        or xy.shape[1] != 2
        or h.shape != (3, 3)
        or not np.isfinite(xy).all()
        or not np.isfinite(h).all()
    ):
        raise ValueError("Projection needs finite points and a 3 by 3 matrix")
    mapped = np.column_stack((xy, np.ones(len(xy)))) @ h.T
    if np.any(np.abs(mapped[:, 2]) < 1e-8):
        raise ValueError("Projection crosses its horizon")
    return mapped[:, :2] / mapped[:, 2:]


def _normalize(points):
    center = points.mean(axis=0)
    radius = np.sqrt(np.mean(np.sum((points - center) ** 2, axis=1)))
    if radius < 1e-8:
        raise ValueError("Coincident calibration points")
    scale = np.sqrt(2) / radius
    matrix = np.array([[scale, 0, -scale * center[0]], [0, scale, -scale * center[1]], [0, 0, 1]])
    return (points - center) * scale, matrix


def _fit(pixels, world):
    p, tp = _normalize(pixels)
    q, tq = _normalize(world)
    rows = []
    for (x, y), (u, v) in zip(p, q, strict=True):
        rows.extend([[-x, -y, -1, 0, 0, 0, u * x, u * y, u], [0, 0, 0, -x, -y, -1, v * x, v * y, v]])
    _, singular, vh = np.linalg.svd(np.array(rows), full_matrices=True)
    # Exactly four correspondences have eight singular values; additional points
    # have nine, with the final value representing the homogeneous fit residual.
    rank_scale = singular[7]
    if rank_scale < 1e-8 or singular[0] / rank_scale > 1e8:
        raise ValueError("Degenerate calibration landmarks")
    h = np.linalg.inv(tq) @ vh[-1].reshape(3, 3) @ tp
    if abs(h[2, 2]) < 1e-8 or not np.isfinite(h).all():
        raise ValueError("Unstable camera homography")
    return h / h[2, 2]


def inside_support(points, hull):
    contour = np.asarray(hull, dtype=np.float32)
    return [cv2.pointPolygonTest(contour, (float(x), float(y)), False) >= 0 for x, y in points]


def fit_calibration(calibration, image_size):
    base = {
        "version": VERSION,
        "status": "unavailable",
        "matrix": None,
        "support_polygon_px": [],
        "fit_errors_m": [],
        "validation_errors_m": [],
        "landmark_errors": [],
        "limitations": [
            "planar_ice_map_only",
            "unmodeled_lens_distortion",
            "handle_height_not_corrected",
            "landmark_residual_is_not_total_physical_error",
        ],
    }
    fit = [p for p in calibration.landmarks if p.use == "fit"]
    validation = [p for p in calibration.landmarks if p.use == "validation"]
    if len(fit) < 4 or len(validation) < 2:
        return {**base, "reasons": ["four_fit_and_two_independent_validation_landmarks_required"]}
    all_points = fit + validation
    if any(not 0 <= p.pixel[0] < image_size[0] or not 0 <= p.pixel[1] < image_size[1] for p in all_points):
        return {**base, "reasons": ["landmark_outside_reference_image"]}
    pixels = np.array([p.pixel for p in fit], dtype=float)
    world = np.array([p.plane_m for p in fit], dtype=float)
    # Reusing a fit location as a holdout is not independent validation.
    if any(np.linalg.norm(np.asarray(v.pixel) - p) < 1 for v in validation for p in pixels):
        return {**base, "reasons": ["validation_reuses_fit_location"]}
    if len({tuple(p.pixel) for p in all_points}) != len(all_points):
        return {**base, "reasons": ["duplicate_landmark_locations"]}
    hull = cv2.convexHull(pixels.astype(np.float32)).reshape(-1, 2)
    if len(hull) < 3 or cv2.contourArea(hull) < image_size[0] * image_size[1] * 0.0001:
        return {**base, "reasons": ["insufficient_landmark_spread"]}
    try:
        matrix = _fit(pixels, world)
        depth = np.column_stack((hull, np.ones(len(hull)))) @ matrix[2]
        if not (np.all(depth > 1e-6) or np.all(depth < -1e-6)):
            raise ValueError("Horizon inside landmark support")
        fit_errors = np.linalg.norm(map_points(pixels, matrix) - world, axis=1)
        val_errors = np.linalg.norm(
            map_points([p.pixel for p in validation], matrix) - np.array([p.plane_m for p in validation]),
            axis=1,
        )
        if not np.isfinite(fit_errors).all() or not np.isfinite(val_errors).all():
            raise ValueError("Unstable residuals")
    except (ValueError, np.linalg.LinAlgError):
        return {**base, "reasons": ["degenerate_or_unstable_homography"]}
    reasons = []
    if calibration.transfer and not calibration.transfer.target_landmarks_reviewed:
        reasons.append("transferred_landmarks_need_independent_target_frame_review")
    if max(fit_errors) > calibration.max_validation_error_m:
        reasons.append("fit_residual_exceeds_tolerance")
    if max(val_errors) > calibration.max_validation_error_m:
        reasons.append("independent_validation_exceeds_tolerance")
    if not all(inside_support([p.pixel for p in validation], hull)):
        reasons.append("validation_outside_supported_landmark_region")
    return {
        **base,
        "status": "review_required" if reasons else "validated_geometry",
        "matrix": matrix.tolist(),
        "support_polygon_px": hull.tolist(),
        "fit_errors_m": fit_errors.tolist(),
        "validation_errors_m": val_errors.tolist(),
        "maximum_validation_error_m": float(max(val_errors)),
        "landmark_errors": [
            {
                "label": point.label,
                "use": point.use,
                "error_m": float(error),
                "within_tolerance": bool(error <= calibration.max_validation_error_m),
            }
            for point, error in zip(all_points, np.concatenate((fit_errors, val_errors)), strict=True)
        ],
        "reasons": reasons,
    }


def project_track(track, calibration, fit):
    base = {
        "track_id": track["id"],
        "calibration_id": calibration.id,
        "status": "unavailable",
        "speed_mps": None,
        "weight_multiplier": None,
        "aim_error_degrees": None,
        "point_convention": "projected_colored_handle_center",
        "dimension_basis": calibration.dimension_basis,
        "acceptance": "proposed",
    }
    if calibration.acceptance != "confirmed" or fit["status"] != "validated_geometry":
        return {**base, "reason": "calibration_not_confirmed_and_validated"}
    if (
        track["view"] != calibration.view
        or not calibration.start_seconds
        <= track["start_seconds"]
        <= track["end_seconds"]
        <= calibration.end_seconds
    ):
        return {**base, "reason": "track_outside_calibration_scope"}
    points = track["points"]
    pixels = [p["center_px"] for p in points]
    if len(points) < 3 or not all(inside_support(pixels, fit["support_polygon_px"])):
        return {**base, "reason": "track_outside_supported_landmark_region"}
    times = np.array([p["source_seconds"] for p in points])
    if np.any(np.diff(times) <= 0) or np.any(np.diff(times) > 0.25):
        return {**base, "reason": "track_timing_gap_or_reversal"}
    coordinates = map_points(pixels, fit["matrix"])
    design = np.column_stack((times - times[0], np.ones(len(times))))
    velocity = np.linalg.lstsq(design, coordinates, rcond=None)[0]
    residuals = coordinates - design @ velocity
    mapped = []
    for p, xy in zip(points, coordinates, strict=True):
        pixel = np.array(p["center_px"])
        probes = pixel + np.array([[2, 0], [-2, 0], [0, 2], [0, -2]])
        sensitivity = float(np.max(np.linalg.norm(map_points(probes, fit["matrix"]) - xy, axis=1)))
        mapped.append({**p, "plane_m": xy.tolist(), "two_pixel_sensitivity_m": sensitivity})
    return {
        **base,
        "status": "experimental_plane_measurement",
        "speed_mps": float(np.linalg.norm(velocity[0])),
        "points": mapped,
        "velocity_plane_mps": velocity[0].tolist(),
        "fit_residual_rms_m": float(np.sqrt(np.mean(np.sum(residuals**2, axis=1)))),
        "notice": "Image-plane estimate with uncorrected handle height. Release, stone association, "
        "sweeping, target speed and called line need separate review; no player rating.",
    }
