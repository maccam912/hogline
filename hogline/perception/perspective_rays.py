"""Approximate ice-plane perspective rays and an independent, paired-stone scale check.

Camera fits use local rings, centre line and tee-to-hog distance, never the
button-to-button distance being checked. Residuals and perturbations are diagnostics,
not a venue survey or statistical confidence interval.
"""

import cv2
import numpy as np
from scipy.optimize import least_squares

from hogline.perception.delivery_ray import RADIUS_M, TEE_DISTANCE_M

VERSION = "perspective-ice-rays-paired-scale-v1"
HOG_FROM_TEE_M = 21 * 0.3048


def project(points, matrix):
    xy = np.asarray(points, dtype=float)
    h = np.asarray(matrix, dtype=float)
    if xy.ndim != 2 or xy.shape[1] != 2 or h.shape != (3, 3):
        raise ValueError("Perspective mapping needs 2D points and a 3 by 3 matrix")
    if not np.isfinite(xy).all() or not np.isfinite(h).all():
        raise ValueError("Perspective mapping must be finite")
    q = np.column_stack((xy, np.ones(len(xy)))) @ h.T
    if np.any(np.abs(q[:, 2]) < 1e-8):
        raise ValueError("Perspective mapping crosses its horizon")
    return q[:, :2] / q[:, 2:]


def register_common_sheet(ray):
    """Register local maps to an ASSUMED nominal span using other-shot pairs."""
    cameras = ray["perspective_calibration"]["cameras"]
    pairs = ray["perspective_calibration"]["pairs"]
    views, initial = list(cameras), []
    for view in views:
        h = np.array(cameras[view]["local_to_pixel"])
        x, y = h[:2, 2]
        g, k = h[2, :2]
        initial.extend([x, y, h[0, 0] - x * g, h[0, 1] - x * k, h[1, 0] - y * g, h[1, 1] - y * k, g, k])
    initial = np.array(initial)
    landmarks = np.array([[x, y] for y in [-1.8288, 0, 1.8288, 6.4008] for x in [-1.8288, 0, 1.8288]])
    reference = {v: project(landmarks, cameras[v]["local_to_pixel"]) for v in views}

    def residual(p):
        maps = {v: _matrix(p[i * 8 : (i + 1) * 8]) for i, v in enumerate(views)}
        errors = []
        for v in views:
            errors.extend(((project(landmarks, maps[v]) - reference[v]) / 0.8).ravel())
        for pair in pairs:
            q = {v: project([pair[v]], np.linalg.inv(maps[v]))[0] for v in views}
            errors.append((sum(a[1] for a in q.values()) - 0.29 - ray["nominal_tee_distance_m"]) / 0.15)
            errors.append(
                (
                    q[views[0]][0] * cameras[views[0]]["transverse_sign"]
                    - q[views[1]][0] * cameras[views[1]]["transverse_sign"]
                )
                / 0.05
            )
        return errors

    delta = np.array([2, 2, 3, 1, 1, 1, 0.004, 0.008] * 2)
    fit = least_squares(
        residual, initial, bounds=(initial - delta, initial + delta), loss="soft_l1", max_nfev=250
    )
    if not fit.success:
        raise ValueError("Common-sheet registration did not converge")
    result = {}
    for i, v in enumerate(views):
        h = _matrix(fit.x[i * 8 : (i + 1) * 8])
        result[v] = {**cameras[v], "local_to_pixel": h.tolist(), "pixel_to_local": np.linalg.inv(h).tolist()}
    return result


def _matrix(p):
    cx, cy, a, b, c, d, g, h = p
    return np.array([[a + cx * g, b + cx * h, cx], [c + cy * g, d + cy * h, cy], [g, h, 1]])


def fit_perspective(rgb, seed, *, boundary_shift_px=0.5, hog_shift_px=0):
    """Fit reviewed, recording-scoped ring/line seeds; exclude every fifth ring point.

    Local X increases image-right. Local Y runs from the visible button towards the
    camera/middle of the sheet. Fit scope is the painted house through the hog line;
    projection beyond it is explicitly an extrapolation.
    """
    rgb = np.asarray(rgb)
    height, width = rgb.shape[:2]
    cx, cy = seed["tee_seed_px"]
    hog = seed["hog_y_seed_px"]
    if not (0 < cx < width and 25 < cy < hog < height - 20):
        raise ValueError("Perspective landmarks lie outside the crop")
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    mask = cv2.inRange(hsv, (90, 60, 45), (140, 255, 255))
    mask[: int(cy - 25)] = 0
    mask[int(cy + 25) :] = 0
    contours, _ = cv2.findContours(mask, cv2.RETR_LIST, cv2.CHAIN_APPROX_NONE)
    contours = sorted(contours, key=cv2.contourArea, reverse=True)
    if len(contours) < 2 or min(cv2.contourArea(c) for c in contours[:2]) < 1000:
        raise ValueError("Two unobscured painted blue boundaries are required")
    rings = []
    for contour, radius, shift in (
        (contours[0], RADIUS_M, boundary_shift_px),
        (contours[1], RADIUS_M * 2 / 3, -boundary_shift_px),
    ):
        points = contour[:, 0].astype(float)
        if len(points) < 200:
            raise ValueError("Insufficient perspective ring support")
        # Contours run through boundary pixel centres. Half a pixel to the
        # actual interface is an assumption; the distance audit varies it.
        normal = (points - np.mean(points, axis=0)) / np.maximum(np.ptp(points, axis=0), 1) ** 2
        normal /= np.linalg.norm(normal, axis=1)[:, None]
        rings.append((points + shift * normal, radius))
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY).astype(float)
    contrast = cv2.GaussianBlur(gray, (15, 1), 0) - gray
    ys = np.arange(int(cy + 30), min(height - 25, int(seed["ice_bottom_px"])))
    xs = contrast[ys, int(cx - 9) : int(cx + 10)].argmax(axis=1) + int(cx - 9)
    keep = contrast[ys, xs] > 1.5
    center = np.column_stack((xs[keep], ys[keep], np.ones(sum(keep))))[::8]
    if len(center) < 20:
        raise ValueError("Visible centre line is required to orient the perspective map")
    # Independently localised hog-line pixels, rather than the nominal inter-button span.
    hog_points = []
    vertical_contrast = cv2.GaussianBlur(gray, (1, 15), 0) - gray
    for x in range(int(cx - 140), int(cx + 141), 20):
        y = int(hog - 4) + np.argmax(vertical_contrast[int(hog - 4) : int(hog + 5), x])
        if vertical_contrast[y, x] > 2:
            hog_points.append([x, int(y + hog_shift_px), 1])
    if len(hog_points) < 8:
        raise ValueError("Visible hog-line support is required")

    def ring_errors(inverse, points, radius):
        p = np.column_stack((points, np.ones(len(points))))
        conic = inverse.T @ np.diag([1, 1, -(radius**2)]) @ inverse
        f = p @ conic
        return np.sum(f * p, axis=1) / np.maximum(np.linalg.norm(2 * f[:, :2], axis=1), 1e-12)

    def residual(parameters):
        inverse = np.linalg.inv(_matrix(parameters))
        errors = []
        for points, radius in rings:
            train = np.arange(len(points)) % 5 != 0
            errors.extend(ring_errors(inverse, points[train], radius))
        for observations, line in ((center, [1, 0, 0]), (hog_points, [0, 1, -HOG_FROM_TEE_M])):
            image_line = inverse.T @ line
            errors.extend(np.asarray(observations) @ image_line / np.linalg.norm(image_line[:2]))
        return np.asarray(errors)

    fit = least_squares(
        residual,
        [cx, cy, 84, 0, 0, 8, 0, -0.025],
        bounds=(
            [cx - 5, cy - 5, 70, -10, -3, 4, -0.03, -0.12],
            [cx + 5, cy + 5, 100, 10, 3, 15, 0.03, 0.02],
        ),
        loss="soft_l1",
        f_scale=0.5,
        max_nfev=500,
    )
    h = _matrix(fit.x)
    inverse = np.linalg.inv(h)
    held_out = np.concatenate([ring_errors(inverse, p[np.arange(len(p)) % 5 == 0], r) for p, r in rings])
    p90 = float(np.quantile(np.abs(held_out), 0.9))
    if not fit.success or p90 > 2.5 or not np.isfinite(h).all():
        raise ValueError("Perspective ring fit failed its held-out pixel check")
    return {
        "local_to_pixel": h.tolist(),
        "pixel_to_local": inverse.tolist(),
        "tee_px": fit.x[:2].tolist(),
        "image_size": [width, height],
        "ice_bottom_px": seed["ice_bottom_px"],
        "house_view": seed["house_view"],
        "transverse_sign": seed["transverse_sign"],
        "status": "approximate_local_ice_plane",
        "fit_region_m": [-RADIUS_M, HOG_FROM_TEE_M],
        "held_out_ring_p90_px": p90,
        "fit_rms_px": float(np.sqrt(np.mean(residual(fit.x) ** 2))),
        "ring_boundary_shift_px": boundary_shift_px,
        "hog_line_pixels": hog_points,
    }


def paired_distance(cameras, pairs, *, stone_radius_m=0.145):
    """Check the span from local metric maps, WITHOUT fitting either to 114 feet.

    Reviewed pixels mark the camera-near bottom granite edge, not the elevated
    handle. Subtract one nominal stone radius in each view to approximate centres.
    """
    if len(pairs) < 3 or not 0 < stone_radius_m < 0.2:
        raise ValueError("Distance check needs three reviewed paired-stone frames")
    result = []
    for pair in pairs:
        local = {v: project([pair[v]], cameras[v]["pixel_to_local"])[0] for v in cameras}
        if any(not 0 < q[1] < 35 for q in local.values()):
            raise ValueError("Paired stone is outside the forward ice region")
        span = sum(p[1] for p in local.values()) - 2 * stone_radius_m
        # A difference, not a sum, in the common transverse coordinate system.
        values = [cameras[v]["transverse_sign"] * local[v][0] for v in cameras]
        lateral = values[0] - values[1]
        result.append(
            {
                "source_seconds": pair["source_seconds"],
                "estimated_tee_distance_m": float(span),
                "transverse_disagreement_m": float(abs(lateral)),
                "local_points_m": {v: p.tolist() for v, p in local.items()},
            }
        )
    return {
        "estimate_m": float(np.median([p["estimated_tee_distance_m"] for p in result])),
        "observed_pair_range_m": [
            min(p["estimated_tee_distance_m"] for p in result),
            max(p["estimated_tee_distance_m"] for p in result),
        ],
        "pairs": result,
        "nominal_stone_radius_m": stone_radius_m,
        "nominal_tee_distance_m": TEE_DISTANCE_M,
        "used_nominal_tee_distance_in_fit": False,
        "decision": "retain_nominal_distance_no_independent_survey",
        "status": "approximate_cross_view_check",
        "limitations": [
            "nominal_painted_rings_and_21ft_tee_to_hog_not_venue_survey",
            "bottom_granite_edge_and_nominal_radius_approximate_ground_centre",
            "lens_distortion_camera_drift_and_possible_camera_time_offsets_unmodeled",
            "paired_views_share_a_broadcast_pts_not_proven_synchronised_exposures",
            "local_maps_extrapolated_beyond_hog_line",
        ],
    }


def ray_segments(ray, camera, *, transverse_offset_m=0):
    """Project the same straight ray into the visible ice, clipping behind-camera points."""
    s = np.linspace(-3.6576, ray["nominal_tee_distance_m"] + 3.6576, 1200)
    x = ray["near_tee_x_m"] + ray["slope_transverse_per_forward"] * s + transverse_offset_m
    own = camera["house_view"] == ray["origin_view"]
    y = s if own else ray["nominal_tee_distance_m"] - s
    world = np.column_stack((camera["transverse_sign"] * x, y, np.ones(len(s))))
    p = world @ np.asarray(camera["local_to_pixel"]).T
    visible = p[:, 2] > 1e-5
    p = p[:, :2] / np.where(np.abs(p[:, 2:]) < 1e-8, 1e-8, p[:, 2:])
    width, _ = camera["image_size"]
    tee_y = camera["tee_px"][1]
    visible &= (p[:, 0] >= 0) & (p[:, 0] <= width)
    visible &= (p[:, 1] >= tee_y - 40) & (p[:, 1] <= camera["ice_bottom_px"])
    # Sheet width is nominal; never paint the continuation onto neighbouring sheets.
    visible &= np.abs(x) <= 2.375
    segments, segment = [], []
    for q, ok in zip(p, visible, strict=True):
        if ok:
            segment.append(q.tolist())
        elif segment:
            if len(segment) > 1:
                segments.append(segment)
            segment = []
    if len(segment) > 1:
        segments.append(segment)
    return segments
