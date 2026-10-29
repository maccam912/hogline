"""Experimental concentric-ring projection for native-image measurement review.

This models the painted ice plane; it is not a surveyed camera calibration and
must not turn handle centroids or stone tops into accepted granite measurements.
"""

import cv2
import numpy as np
from scipy.optimize import least_squares

VERSION = "concentric-painted-house-projection-v1"
RADII = (1 / 3, 2 / 3, 1.0)


def project_points(points, matrix, origin, scale):
    points = np.asarray(points, dtype=float)
    if points.ndim != 2 or points.shape[1] != 2 or not np.all(np.isfinite(points)):
        raise ValueError("Projection needs finite two-dimensional points")
    matrix, origin = np.asarray(matrix, dtype=float), np.asarray(origin, dtype=float)
    if (
        matrix.shape != (3, 3)
        or origin.shape != (2,)
        or not np.all(np.isfinite(matrix))
        or not np.all(np.isfinite(origin))
        or not np.isfinite(scale)
        or scale <= 0
    ):
        raise ValueError("Projection needs a finite matrix, origin and positive scale")
    coordinates = np.column_stack(((points - origin) / scale, np.ones(len(points))))
    mapped = coordinates @ matrix.T
    if np.any(np.abs(mapped[:, 2]) < 1e-8):
        raise ValueError("Projection crosses its horizon")
    return mapped[:, :2] / mapped[:, 2:]


def _matrix(parameters):
    # Fix the irrelevant rotation gauge with an upper-right zero.
    a, d, e, c, f, g, h = parameters
    return np.array([[a, 0, c], [d, e, f], [g, h, 1.0]])


def diagnose_house_projection(rgb):
    """Fit all three colored boundaries; return diagnostics, never an end score."""
    rgb = np.asarray(rgb)
    if rgb.ndim != 3 or rgb.shape[2] != 3 or rgb.dtype != np.uint8:
        raise ValueError("House projection needs an RGB uint8 image")
    height, width = rgb.shape[:2]
    if min(width, height) < 160 or max(width, height) > 4096:
        raise ValueError("House projection expects a native house crop, 160–4096 pixels per side")
    origin = np.array([width / 2, height / 2])
    scale = min(width, height) * 215 / 480
    base = {
        "version": VERSION,
        "acceptance": "proposed",
        "image_size": [width, height],
        "origin_px": origin.tolist(),
        "scale_px": scale,
        "ring_radii_house_units": list(RADII),
        "metric_calibration": None,
        "provisional_score": None,
        "limitations": [
            "standard_concentric_painted_ring_ratios_assumed",
            "painted_ice_plane_only; stone_height_and_lens_distortion_not_calibrated",
            "ring_residual_is_not_physical_measurement_accuracy",
            "no_stone_object_or_granite_boundary_verification",
        ],
    }
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    blue = cv2.inRange(hsv, (90, 60, 45), (140, 255, 255))
    green = cv2.inRange(hsv, (42, 60, 40), (88, 255, 255))
    pixels, radii = [], []
    for mask, choices in ((blue, np.array([2 / 3, 1.0])), (green, np.array([1 / 3]))):
        contours, _ = cv2.findContours(mask, cv2.RETR_LIST, cv2.CHAIN_APPROX_NONE)
        for contour in contours:
            if cv2.contourArea(contour) < width * height / 230.4:
                continue
            candidate = contour[:, 0].astype(float)
            normalized = (candidate - origin) / scale
            radius = np.linalg.norm(normalized, axis=1)
            nearest = np.argmin(np.abs(radius[:, None] - choices), axis=1)
            keep = (
                (np.abs(radius - choices[nearest]) < 0.07)
                & np.all(candidate > 5, axis=1)
                & np.all(candidate < np.array([width, height]) - 5, axis=1)
            )
            pixels.extend(candidate[keep])
            radii.extend(choices[nearest[keep]])
    pixels, radii = np.asarray(pixels), np.asarray(radii)
    counts = {str(r): int(np.sum(radii == r)) for r in RADII}
    base["ring_candidate_points"] = counts
    if min(counts.values()) < 50:
        return {**base, "status": "unavailable", "reasons": ["three_painted_boundaries_required"]}
    normalized = (pixels - origin) / scale

    def residual(parameters):
        projected = project_points(pixels, _matrix(parameters), origin, scale)
        return np.linalg.norm(projected, axis=1) - radii

    # Spatially held-out angular sectors check extrapolation through gaps in the fit.
    sectors = np.floor((np.arctan2(normalized[:, 1], normalized[:, 0]) + np.pi) * 36 / (2 * np.pi))
    sectors = sectors.astype(int) % 36
    training = sectors % 5 != 0
    held_out = ~training
    parameters = np.array([1.0, 0, 1.0, 0, 0, 0, 0])
    inliers = np.ones(len(pixels), dtype=bool)
    try:
        for _ in range(4):
            if np.count_nonzero(training & inliers) < 150:
                raise ValueError("Too few ring points")
            fit = least_squares(
                lambda values, selected=training & inliers: residual(values)[selected],
                parameters,
                loss="soft_l1",
                f_scale=0.003,
                bounds=([0.8, -0.2, 0.8, -0.1, -0.1, -0.15, -0.15], [1.2, 0.2, 1.2, 0.1, 0.1, 0.15, 0.15]),
                max_nfev=100,
            )
            parameters = fit.x
            inliers = np.abs(residual(parameters)) < 0.012
        if not np.any(inliers):
            raise ValueError("No supported ring points")
        matrix = _matrix(parameters)
        corners = np.array([[0, 0], [width, 0], [width, height], [0, height]])
        depth = 1 + ((corners - origin) / scale) @ parameters[-2:]
        inverse = np.linalg.inv(matrix)
        tee = inverse @ [0, 0, 1]
        tee = tee[:2] / tee[2] * scale + origin
    except (ValueError, np.linalg.LinAlgError):
        return {**base, "status": "unavailable", "reasons": ["projection_fit_failed"]}
    errors = np.abs(residual(parameters))
    diagnostics = []
    reasons = []
    for radius in RADII:
        selected = radii == radius
        good = selected & inliers
        held_out_ring = selected & held_out
        validation = held_out_ring & inliers
        validation_fraction = float(np.mean(inliers[held_out_ring])) if np.any(held_out_ring) else 0.0
        coverage = len(np.unique(sectors[good]))
        fraction = float(np.mean(inliers[selected]))
        validation_error = float(np.quantile(errors[validation], 0.9)) if np.any(validation) else None
        diagnostics.append(
            {
                "radius": radius,
                "inlier_fraction": fraction,
                "angular_sectors_of_36": coverage,
                "held_out_inlier_fraction": validation_fraction,
                "held_out_inlier_points": int(np.sum(validation)),
                "held_out_inlier_p90_residual": validation_error,
            }
        )
        if (
            fraction < 0.7
            or coverage < 24
            or validation_fraction < 0.7
            or np.sum(validation) < 15
            or validation_error > 0.01
        ):
            reasons.append("insufficient_ring_support_or_held_out_coverage")
    if not fit.success or np.any(depth < 0.7) or not np.all(np.isfinite(tee)):
        reasons.append("unstable_projection")
    return {
        **base,
        "status": "review_required" if reasons else "candidate",
        "reasons": sorted(set(reasons)),
        "pixel_normalized_to_house": matrix.tolist(),
        "tee_px": tee.tolist(),
        "ring_diagnostics": diagnostics,
        "inlier_median_residual": float(np.median(errors[inliers])),
        "holdout": "every_fifth_angular_sector; not an independent recording evaluation",
    }
