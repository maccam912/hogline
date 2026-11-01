"""Bounded multi-view stone tracking; inferred gaps never become observed positions."""

import cv2
import numpy as np
from scipy.optimize import lsq_linear

from hogline.perception.delivery_ray import ice_points, image_points
from hogline.perception.perspective_rays import project
from hogline.perception.stones import RawHSVHandleDetector

VERSION = "multi-view-throw-reconstruction-v2"
BOUNDS = {
    "house_bottom": [480, 400, 800, 720],
    "house_top": [480, 0, 800, 320],
    "perspective_left": [0, 0, 480, 684],
    "perspective_right": [800, 0, 1280, 684],
}


def tee_time(ray):
    points = ray["points"]
    progress = np.array([ray["forward_sign"] * p["ice_m"][1] for p in points])
    for i in range(1, len(points)):
        if progress[i - 1] <= 0 < progress[i]:
            f = -progress[i - 1] / (progress[i] - progress[i - 1])
            return float(points[i - 1]["t"] + f * (points[i]["t"] - points[i - 1]["t"]))
    raise ValueError("No observed delivery-tee crossing; common replay alignment is unavailable")


def orientation(ray):
    return 1 if ray["origin_view"] == "house_bottom" else -1


def world(pixel, view, ray, cameras, ground=False):
    distance = ray["nominal_tee_distance_m"]
    if view in cameras:
        camera = cameras[view]
        q = project([pixel], camera["pixel_to_local"])[0]
        depth = q[1] - (0.145 if ground else 0)
        s = depth if camera["house_view"] == ray["origin_view"] else distance - depth
        x = camera["transverse_sign"] * q[0]
    else:
        geometry = ray["geometry"] if view == ray["origin_view"] else ray["receiving_geometry"]
        q = ice_points([pixel], geometry)[0]
        x, s = q[0], ray["forward_sign"] * q[1]
        if view != ray["origin_view"]:
            s += distance
    return [float(x * orientation(ray)), float(s)]


def ground_candidates(rgb):
    """Yellow-handle/gray-body proposals, with merged clothing rejected."""
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    yellow = cv2.inRange(hsv, (17, 65, 100), (40, 255, 255))
    yellow[:275] = 0
    yellow[655:] = 0
    n, _, boxes, centers = cv2.connectedComponentsWithStats(yellow)
    found = []
    for i in range(1, n):
        _, _, w, h, area = boxes[i]
        x, y = centers[i]
        if not (3 <= area <= 400 and 2 <= w <= 35 and 1 <= h <= 20):
            continue
        x0, x1 = max(0, round(x) - 38), min(rgb.shape[1], round(x) + 39)
        y0, y1 = round(y) + 2, min(rgb.shape[0], round(y) + 38)
        if y0 >= y1:
            continue
        gray = cv2.inRange(hsv[y0:y1, x0:x1], (0, 0, 60), (179, 85, 180))
        gray = cv2.morphologyEx(gray, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
        count, _, components, _ = cv2.connectedComponentsWithStats(gray)
        choices = []
        for j in range(1, count):
            bx, by, bw, bh, body_area = components[j]
            cx, bottom = x0 + bx + bw / 2, y0 + by + bh - 0.5
            if (
                8 <= bw <= 65
                and 3 <= bh < 34
                and body_area >= 25
                and bx <= x - x0 <= bx + bw
                and by < 14
                and abs(cx - x) < 12
            ):
                choices.append((abs(cx - x) + by, [float(cx), float(bottom)]))
        if choices:
            found.append(min(choices)[1])
    return found[:64]


def frame_candidates(image, ray, cameras):
    rows = []
    for view, bounds in BOUNDS.items():
        crop = image.crop(bounds)
        rgb = np.asarray(crop)
        if view in cameras:
            detections = ground_candidates(rgb)
        else:
            detections = [
                p["center_px"]
                for p in RawHSVHandleDetector().detect(rgb)
                if p["color"] == "yellow" and 30 <= p["area_px2"] <= 600
            ]
        for pixel in detections:
            q = world(pixel, view, ray, cameras, ground=view in cameras)
            if -3 < q[1] < ray["nominal_tee_distance_m"] + 3 and abs(q[0]) < 3:
                rows.append(
                    {
                        "pixel": pixel,
                        "view": view,
                        "world_m": q,
                        "kind": "perspective_granite_edge" if view in cameras else "overhead_handle",
                    }
                )
    return rows


def associate(rows, ray, finish="unknown"):
    """Follow the seeded moving stone; stationary receiving stones cannot start a track."""
    native = [
        {
            **p,
            "view": ray["origin_view"],
            "world_m": [orientation(ray) * p["ice_m"][0], ray["forward_sign"] * p["ice_m"][1]],
            "kind": "native_delivery_handle",
        }
        for p in ray["points"]
    ]
    observations = list(native)
    last = {"t": native[-1]["t"], "q": np.array(native[-1]["world_m"])}
    previous = native[max(0, len(native) - 20)]
    velocity = (last["q"] - previous["world_m"]) / (last["t"] - previous["t"])
    velocity[1] = np.clip(velocity[1], 0.5, 6)
    pre_arrival = tee_time(ray) + max(3, (ray["nominal_tee_distance_m"] - 5) / max(velocity[1] * 1.5, 3))
    baseline = {}
    for row in rows[:4]:
        for p in row["detections"]:
            baseline.setdefault(p["view"], []).append(p["world_m"])
    moving_received = False
    ambiguous_frames = 0
    for row in rows:
        t = row["t"]
        dt = t - last["t"]
        if dt <= 0:
            continue
        if dt > 20 or (moving_received and dt > 3):
            break
        prediction = last["q"] + velocity * dt
        prediction[0] = last["q"][0] + velocity[0] * min(dt, 4)
        prediction[1] -= 0.5 * 0.065 * dt**2
        options = []
        for p in row["detections"]:
            if p["view"] == ray["origin_view"]:
                continue
            q = np.array(p["world_m"])
            if t < pre_arrival:
                baseline.setdefault(p["view"], []).append(p["world_m"])
            if finish == "in_front_of_rings" and q[1] > ray["nominal_tee_distance_m"] - 1.8288 + 0.4:
                continue
            longitudinal = abs(q[1] - prediction[1])
            lateral = abs(q[0] - prediction[0])
            near = p["kind"] == "overhead_handle"
            if longitudinal > min(8, 1.6 + dt * 0.8) or lateral > min(2.5, 0.35 + dt * 0.28):
                continue
            if q[1] < last["q"][1] - (0.5 if near else 1.0):
                continue
            if not moving_received and (near or q[1] > 20):
                if any(np.linalg.norm(q - old) < 0.13 for old in baseline.get(p["view"], [])):
                    continue
            score = (longitudinal / ((1 if near else 1.7) + 0.1 * dt)) ** 2 + (
                lateral / (0.2 + 0.12 * min(dt, 8))
            ) ** 2
            options.append((score, p))
        if not options:
            continue
        options.sort(key=lambda item: item[0])
        if (
            len(options) > 1
            and options[1][0] - options[0][0] < 0.5
            and options[0][1]["view"] == options[1][1]["view"]
            and np.linalg.norm(np.array(options[0][1]["world_m"]) - options[1][1]["world_m"]) > 0.4
        ):
            ambiguous_frames += 1
            continue
        best = options[0][1]
        q = np.array(best["world_m"])
        if best["kind"] == "overhead_handle":
            moving_received = True
        # Camera depth errors must not become instantaneous velocity jumps.
        measured = (q - last["q"]) / dt
        if dt < 2:
            velocity = 0.85 * velocity + 0.15 * measured
        else:
            velocity = 0.6 * velocity + 0.4 * measured
        velocity[0] = np.clip(velocity[0], -0.35, 0.35)
        velocity[1] = np.clip(velocity[1], 0, 6)
        observations.append({**best, "t": t, **row["timing"], "frame_sha256": row["frame_sha256"]})
        last = {"q": q, "t": t}
        # End only after a sustained observed stationary receiving position.
        received = [p for p in observations[-16:] if p["view"] == best["view"] and p["world_m"][1] > 20]
        if len(received) >= 10 and received[-1]["t"] - received[0]["t"] >= 2.25:
            xy = np.array([p["world_m"] for p in received])
            tolerance = 0.1 if best["kind"] == "overhead_handle" else 0.25
            if np.max(np.linalg.norm(xy - np.median(xy, axis=0), axis=1)) < tolerance:
                observations[-1]["apparent_rest"] = True
                break
    observations[-1]["ambiguous_frames_skipped"] = ambiguous_frames
    return observations


def smooth(observations, ray, contact=False):
    times = np.array([p["t"] for p in observations])
    start, end = float(times.min()), float(times.max())
    step = 0.25
    grid = np.linspace(start, end, max(3, int(np.ceil((end - start) / step)) + 1))
    step = grid[1] - grid[0]
    n = len(grid)
    a = np.zeros((len(times), n))
    for i, t in enumerate(times):
        j = min(n - 2, max(0, int((t - start) / step)))
        f = (t - grid[j]) / step
        a[i, j], a[i, j + 1] = 1 - f, f
    points = np.array([p["world_m"] for p in observations])
    overhead = np.array([p["view"].startswith("house_") for p in observations])
    d2 = np.diff(np.eye(n), n=2, axis=0) / step**2
    values = []
    for axis in range(2):
        w = np.where(overhead, 1 / (0.045 if axis == 0 else 0.1), 1 / (0.18 if axis == 0 else 0.65))
        system = np.vstack((a * w[:, None], d2 / (0.16 if axis == 0 else 0.8)))
        rhs = np.r_[points[:, axis] * w, np.zeros(len(d2))]
        if axis == 0 or contact:
            fitted = np.linalg.lstsq(system, rhs, rcond=None)[0]
        else:
            cumulative = np.tril(np.ones((n, n)))
            low, high = np.zeros(n), np.full(n, step * 8)
            low[0], high[0] = -5, 1
            result = lsq_linear(system @ cumulative, rhs, bounds=(low, high), max_iter=100)
            if not result.success:
                raise ValueError("Trajectory fit did not converge")
            fitted = cumulative @ result.x
        values.append(fitted)
    crossing = tee_time(ray)
    output = np.column_stack((grid - crossing, *values))
    residual = a @ output[:, 1:] - points
    supported = sorted(set(float(t - crossing) for t in times))
    last = observations[-1]
    received = [p for p in observations if p["view"] == ray["receiving_view"] and p["t"] > end - 2.5]
    stationary = bool(last.get("apparent_rest"))
    if len(received) >= 8 and received[-1]["t"] - received[0]["t"] >= 1.75:
        xy = np.array([p["world_m"] for p in received])
        stationary = stationary or np.max(np.linalg.norm(xy - np.median(xy, axis=0), axis=1)) < 0.1
    return {
        "grid": output.tolist(),
        "observation_times": supported,
        "alignment_source_seconds": crossing,
        "duration_seconds": float(output[-1, 0]),
        "status": "apparent_rest"
        if stationary and not contact
        else "contact_uncertain"
        if contact
        else "partial_track",
        "last_observed_view": last["view"],
        "observations": len(observations),
        "ambiguous_frames_skipped": int(last.get("ambiguous_frames_skipped", 0)),
        "longest_gap_seconds": float(np.max(np.diff(sorted(set(times))))),
        "median_fit_residual_m": np.median(np.abs(residual), axis=0).tolist(),
        "notice": "Approximate camera reconstruction. Dashed sections are inferred; "
        "unknown tails are not stationary finishes. No called-target accuracy or calibrated probability.",
    }


def video_maps(ray, cameras):
    """Camera-to-common-plane matrices for the browser's video warp."""
    result = []
    for view, bounds in BOUNDS.items():
        sign = orientation(ray)
        own = view == ray["origin_view"] or (
            view in cameras and cameras[view]["house_view"] == ray["origin_view"]
        )
        if view in cameras:
            c = cameras[view]
            local = np.array(
                [
                    [c["transverse_sign"] * sign, 0, 0],
                    [0, 1 if own else -1, 0 if own else ray["nominal_tee_distance_m"]],
                    [0, 0, 1],
                ]
            )
            matrix = np.array(c["local_to_pixel"]) @ local
        else:
            g = ray["geometry"] if own else ray["receiving_geometry"]
            src = np.array([[-1, -1], [1, -1], [1, 1], [-1, 1]], np.float32)
            h = cv2.getPerspectiveTransform(src, image_points(src, g).astype(np.float32))
            local = np.array(
                [
                    [sign, 0, 0],
                    [
                        0,
                        ray["forward_sign"],
                        0 if own else -ray["forward_sign"] * ray["nominal_tee_distance_m"],
                    ],
                    [0, 0, 1],
                ]
            )
            matrix = h @ local
        result.append(
            {
                "view": view,
                "matrix": matrix.tolist(),
                "bounds": bounds,
                "perspective": view in cameras,
                "own": own,
            }
        )
    return result
