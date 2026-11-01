"""Local tangent at the delivery hog, from source observations rather than a global spline."""

import numpy as np

from hogline.perception.perspective_rays import HOG_FROM_TEE_M

VERSION = "delivery-hog-local-tangent-v1"


def fit_hog_ray(points, tee_distance_m, *, half_window_m=1.0):
    """Quadratic local path: evaluate its derivative exactly at the hog plane."""
    selected = [p for p in points if abs(p["world_m"][1] - HOG_FROM_TEE_M) <= half_window_m]
    if len(selected) < 12:
        raise ValueError("Too few observations near the delivery hog line")
    selected.sort(key=lambda p: p["t"])
    xy = np.array([p["world_m"] for p in selected])
    u, x = xy[:, 1] - HOG_FROM_TEE_M, xy[:, 0]
    if u.min() > -0.25 or u.max() < 0.25:
        raise ValueError("Observed positions must bracket the delivery hog line")
    if np.max(np.diff([p["t"] for p in selected])) > 0.2:
        raise ValueError("Hog-line observations have an unsupported gap")
    keep = np.ones(len(u), dtype=bool)
    for _ in range(3):
        coef = np.polyfit(u[keep], x[keep], 2)
        residual = x - np.polyval(coef, u)
        keep = np.abs(residual) <= max(0.025, 4 * np.median(np.abs(residual[keep])))
        if keep.sum() < 12:
            raise ValueError("Unstable hog-line handle track")
    coef = np.polyfit(u[keep], x[keep], 2)
    if u[keep].min() > -0.25 or u[keep].max() < 0.25:
        raise ValueError("Filtered positions no longer bracket the hog line")
    bend, slope, at_hog = map(float, coef)
    target = at_hog + slope * (tee_distance_m - HOG_FROM_TEE_M)
    # Changing the local window reveals sensitivity to motion/measurement noise.
    targets = [target]
    for fraction in (0.7, 0.85):
        k = keep & (abs(u) <= half_window_m * fraction)
        if k.sum() >= 12 and u[k].min() < -0.2 and u[k].max() > 0.2:
            _, b, a = np.polyfit(u[k], x[k], 2)
            targets.append(float(a + b * (tee_distance_m - HOG_FROM_TEE_M)))
    times = np.array([p["t"] for p in selected])
    i = next((i for i in range(1, len(u)) if u[i - 1] <= 0 < u[i]), None)
    if i is None:
        raise ValueError("No forward hog crossing")
    f = -u[i - 1] / (u[i] - u[i - 1])
    return {
        "version": VERSION,
        "status": "estimated_hog_tangent",
        "crossing_seconds": float(times[i - 1] + f * (times[i] - times[i - 1])),
        "hog_x_m": at_hog,
        "slope_transverse_per_forward": slope,
        "target_tee_x_m": target,
        "near_tee_x_m": at_hog - slope * HOG_FROM_TEE_M,
        "quadratic_coefficient": bend,
        "fit_point_count": int(keep.sum()),
        "observed_span_m": [float(u[keep].min()), float(u[keep].max())],
        "window_tee_crossings_m": targets,
        "fit_rms_m": float(np.sqrt(np.mean((x[keep] - np.polyval(coef, u[keep])) ** 2))),
        "points": selected,
        "notice": "Local tangent estimated from a roughly two-metre segment around the hog. "
        "Handle positions use a local granite-edge offset; camera geometry and handle height "
        "remain approximate. Window sensitivity is not a confidence interval.",
    }
