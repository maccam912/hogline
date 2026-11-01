"""Delivery-house trajectories and explicitly straight, nominal-distance tee projections."""

import math

import cv2
import numpy as np

from hogline.perception.projective_house import diagnose_house_projection, project_points

VERSION = "native-house-transit-straight-ray-v2"
RADIUS_M = 6 * 0.3048
TEE_DISTANCE_M = 114 * 0.3048
HACK_BEHIND_TEE_M = 12 * 0.3048
DIMENSION_REFERENCE = "https://worldcurling.org/wp-content/uploads/2026/10/Rules-of-Curling-2026.pdf"


def yellow_leading_edges(rgb, forward_sign):
    """Alternative proposals when a yellow sleeve joins the handle. Review required.

    The leading nine-pixel cap of a yellow component approximates the handle,
    not the granite footprint. It can also be clothing and is never an identity.
    """
    mask = cv2.inRange(cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV), (17, 65, 100), (40, 255, 255))
    mask[:, :40] = 0
    mask[:, -40:] = 0
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask)
    result = []
    for k in range(1, count):
        _, _, width, _, area = stats[k]
        if area < 30 or width > 90:
            continue
        ys, xs = np.where(labels == k)
        front = int(ys.min() if forward_sign == -1 else ys.max())
        cap = (ys - front) * forward_sign > -9
        result.append(
            {
                "center_px": [float(np.mean(xs[cap])), float(front - forward_sign * 5)],
                "area_px2": 80,
                "component_area_px2": int(area),
            }
        )
    return result


def ice_points(points, geometry):
    p = geometry["projection"]
    xy = project_points(points, p["pixel_normalized_to_house"], p["origin_px"], p["scale_px"])
    return xy @ np.asarray(geometry["rotation"]).T * RADIUS_M


def image_points(points, geometry):
    p = geometry["projection"]
    xy = np.asarray(points) @ np.asarray(geometry["rotation"]) / RADIUS_M
    q = np.column_stack((xy, np.ones(len(xy)))) @ np.linalg.inv(p["pixel_normalized_to_house"]).T
    return q[:, :2] / q[:, 2:] * p["scale_px"] + p["origin_px"]


def house_geometry(rgb):
    projection = diagnose_house_projection(rgb)
    if "pixel_normalized_to_house" not in projection:
        raise ValueError("Visible painted rings are required for the house plane")
    # The older strict three-ring gate remains unchanged. A separate approximate
    # product can use well-supported outer boundaries and retain the failed inner check.
    outer = [r for r in projection["ring_diagnostics"] if r["radius"] >= 0.66]
    if len(outer) != 2 or any(
        r["angular_sectors_of_36"] < 30
        or r["held_out_inlier_fraction"] < 0.7
        or r["held_out_inlier_p90_residual"] > 0.012
        for r in outer
    ):
        raise ValueError("Outer painted boundaries do not support even an approximate house plane")
    cx, cy = projection["tee_px"]
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY).astype(float)
    smooth = cv2.GaussianBlur(gray, (11, 1), 0)
    contrast = smooth - gray
    samples = []
    for y in range(15, len(gray) - 15):
        # Button/painted boundaries cannot independently establish a straight centre line.
        distance = abs(y - cy)
        if distance < 22 or any(abs(distance - projection["scale_px"] * r) < 7 for r in (1 / 3, 2 / 3, 1)):
            continue
        lo, hi = max(1, int(cx - 8)), min(gray.shape[1] - 1, int(cx + 9))
        x = lo + int(np.argmax(contrast[y, lo:hi]))
        if contrast[y, x] > 1.5:
            samples.append([x, y])
    rotation = np.eye(2)
    orientation = "image_vertical_assumption"
    if len(samples) >= 60 and np.ptp(np.asarray(samples)[:, 1]) >= 160:
        a = np.asarray(samples)
        slope, intercept = np.polyfit(a[:, 1], a[:, 0], 1)
        keep = np.abs(a[:, 0] - (a[:, 1] * slope + intercept)) < 1.5
        if np.sum(keep) >= 50:
            slope, intercept = np.polyfit(a[keep, 1], a[keep, 0], 1)
            q = project_points(
                [[intercept + slope * 30, 30], [intercept + slope * 290, 290]],
                projection["pixel_normalized_to_house"],
                projection["origin_px"],
                projection["scale_px"],
            )
            axis = q[1] - q[0]
            axis /= np.linalg.norm(axis)
            rotation = np.array([[axis[1], -axis[0]], axis])
            orientation = "visible_centre_line_fit"
    return {
        "projection": projection,
        "rotation": rotation.tolist(),
        "orientation": orientation,
        "status": "approximate_painted_ice_plane",
        "dimension_basis": "nominal_rules_not_wausau_survey",
    }


def directional_tracks(rows, forward_sign, *, max_gap=0.25):
    """Link colored candidates through the house; clothing/objects still need visual review."""
    tracks = []
    for frame in rows:
        found = [d for d in frame["detections"] if 15 <= d["area_px2"] <= 500]
        used = set()
        for track in sorted(tracks, key=len, reverse=True):
            last = track[-1]
            dt = frame["t"] - last["t"]
            if not 0 < dt <= max_gap:
                continue
            velocity = np.array([0.0, forward_sign * 180])
            if len(track) >= 3:
                earlier = track[-3]
                velocity = (np.array(last["pixel"]) - earlier["pixel"]) / (last["t"] - earlier["t"])
            predicted = np.asarray(last["pixel"]) + velocity * dt
            choices = []
            for index, detection in enumerate(found):
                if index in used:
                    continue
                pixel = detection["center_px"]
                dx = pixel[0] - last["pixel"][0]
                progress = (pixel[1] - last["pixel"][1]) * forward_sign
                error = np.linalg.norm(np.asarray(pixel) - predicted)
                if -2.5 <= progress <= dt * 500 + 5 and abs(dx) <= dt * 100 + 4 and error <= 15 + dt * 150:
                    choices.append((error, index, pixel))
            if choices:
                _, index, pixel = min(choices)
                used.add(index)
                track.append({"t": frame["t"], "pts": frame["pts"], "pixel": pixel})
        for index, detection in enumerate(found):
            if index not in used:
                tracks.append([{"t": frame["t"], "pts": frame["pts"], "pixel": detection["center_px"]}])
    return sorted(
        [t for t in tracks if len(t) >= 4 and (t[-1]["pixel"][1] - t[0]["pixel"][1]) * forward_sign > 120],
        key=lambda t: -(t[-1]["pixel"][1] - t[0]["pixel"][1]) * forward_sign,
    )


def crossing(points, forward_m, plane):
    for i in range(1, len(points)):
        if forward_m[i - 1] <= plane < forward_m[i]:
            a, b = points[i - 1], points[i]
            fraction = (plane - forward_m[i - 1]) / (forward_m[i] - forward_m[i - 1])
            return {
                "estimated_seconds": a["t"] + fraction * (b["t"] - a["t"]),
                "frame_before_pts": a["pts"],
                "frame_after_pts": b["pts"],
                "bracket_seconds": [a["t"], b["t"]],
                "after_sample_index": i,
                "extrapolated": False,
            }
    return None


def estimated_edge_crossing(points, forward_m, plane):
    """A small crop-edge gap can be estimated, never described as an observed frame."""
    before = plane < forward_m[0]
    gap = forward_m[0] - plane if before else plane - forward_m[-1]
    if not 0 <= gap <= 0.35:
        return None
    indices = np.arange(min(12, len(points))) if before else np.arange(max(0, len(points) - 12), len(points))
    times = np.array([points[i]["t"] for i in indices])
    speed, position = np.polyfit(times - times[0], forward_m[indices], 1)
    if speed <= 0:
        return None
    seconds = float(times[0] + (plane - position) / speed)
    endpoint = points[0] if before else points[-1]
    if abs(seconds - endpoint["t"]) > 0.15:
        return None
    return {
        "estimated_seconds": seconds,
        "extrapolated": True,
        "outside_observed_span_m": float(gap),
        "anchor_pts": [points[i]["pts"] for i in indices],
        "method": "local_linear_edge_extrapolation_max_0.35m_0.15s",
    }


def fit_ray(
    points, geometry, forward_sign, *, source_fps=60, tee_distance_m=TEE_DISTANCE_M, pixel_sensitivity=1.5
):
    if forward_sign not in (-1, 1) or not math.isfinite(tee_distance_m) or not 20 < tee_distance_m < 50:
        raise ValueError("Ray needs a physical travel direction and nominal tee distance")
    if len(points) < 12:
        raise ValueError("Too few native observations for a delivery-house ray")
    if not math.isfinite(source_fps) or not 1 <= source_fps <= 60 or not 1 <= pixel_sensitivity <= 10:
        raise ValueError("Unsupported source cadence or pixel sensitivity")
    times = np.array([p["t"] for p in points])
    pts = np.array([p["pts"] for p in points])
    if (
        not np.all(np.isfinite(times))
        or np.any(np.diff(pts) <= 0)
        or np.any(np.diff(times) <= 0)
        or np.max(np.diff(times)) > 0.15
    ):
        raise ValueError("Trajectory has a timing reversal or unsupported gap")
    frame_steps = np.diff(times) * source_fps
    if not np.allclose(frame_steps, np.round(frame_steps), atol=0.0001):
        raise ValueError("Variable source cadence cannot be reported as a native frame count")
    xy = ice_points([p["pixel"] for p in points], geometry)
    s, x = forward_sign * xy[:, 1], xy[:, 0]
    if s[-1] - s[0] < 1.5 or np.any(np.diff(s) < -0.05):
        raise ValueError("Insufficient continuous forward house travel")
    keep = np.ones(len(points), dtype=bool)
    for _ in range(3):
        slope, intercept = np.polyfit(s[keep], x[keep], 1)
        residual = x - (s * slope + intercept)
        keep = np.abs(residual) <= max(0.025, float(np.median(np.abs(residual[keep]))) * 4)
    if np.sum(keep) < 12 or np.mean(keep) < 0.75:
        raise ValueError("Competing detections or an unstable delivery track")
    predicted = float(intercept + slope * tee_distance_m)
    halves = []
    for indices in (np.flatnonzero(keep)[: np.sum(keep) // 2], np.flatnonzero(keep)[np.sum(keep) // 2 :]):
        b, a = np.polyfit(s[indices], x[indices], 1)
        halves.append(float(a + b * tee_distance_m))
    sensitivity = 2 * pixel_sensitivity * RADIUS_M / geometry["projection"]["scale_px"]
    sensitivity *= (tee_distance_m - float(np.mean(s))) / float(np.ptp(s))
    sensitivity += tee_distance_m * pixel_sensitivity / (2 * geometry["projection"]["scale_px"])
    bounds = [min(predicted - sensitivity, *halves), max(predicted + sensitivity, *halves)]
    entry, exit_ = crossing(points, s, -RADIUS_M), crossing(points, s, RADIUS_M)
    entry = entry or estimated_edge_crossing(points, s, -RADIUS_M)
    exit_ = exit_ or estimated_edge_crossing(points, s, RADIUS_M)
    split = {"status": "partial_house_only", "entry": entry, "exit": exit_}
    if entry and exit_:
        elapsed = exit_["estimated_seconds"] - entry["estimated_seconds"]
        split.update(
            status="estimated_full_house"
            if entry["extrapolated"] or exit_["extrapolated"]
            else "observed_full_house",
            elapsed_seconds=elapsed,
            source_frame_difference=round(elapsed * source_fps)
            if entry["extrapolated"] or exit_["extrapolated"]
            else round((exit_["bracket_seconds"][1] - entry["bracket_seconds"][1]) * source_fps),
            nominal_mean_speed_mps=2 * RADIUS_M / elapsed,
        )
    return {
        "version": VERSION,
        "status": "estimated_house_ray",
        "point_count": len(points),
        "first_seconds": float(times[0]),
        "last_seconds": float(times[-1]),
        "forward_sign": forward_sign,
        "source_fps": source_fps,
        "nominal_tee_distance_m": tee_distance_m,
        "target_tee_x_m": predicted,
        "target_tee_sensitivity_m": bounds,
        "pixel_sensitivity_assumption": pixel_sensitivity,
        "slope_transverse_per_forward": float(slope),
        "near_tee_x_m": float(intercept),
        "near_hack_ray_x_m": float(intercept - slope * HACK_BEHIND_TEE_M),
        "early_late_tee_x_m": halves,
        "residual_rms_m": float(np.sqrt(np.mean(residual[keep] ** 2))),
        "observed_forward_span_m": [float(s.min()), float(s.max())],
        "house_split": split,
        "points": [{**p, "ice_m": q.tolist()} for p, q in zip(points, xy, strict=True)],
        "interpretation": "Straight extrapolation of a possibly hand-controlled delivery-house path; "
        "not release aim, curling path, final destination, or a calibrated confidence interval.",
    }
