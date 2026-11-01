"""One reviewed shot: native clip, planar mosaic, and retrospective 2D track.

Run with `uv run python scripts/shot_reconstruction_example.py prepare|render`.
This is a source-scoped demonstration, not an automatic analysis campaign.
All media/observations/exports stay in ignored data/shot-reconstruction/example/.
"""

import argparse
import functools
import hashlib
import json
import subprocess
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from scipy.interpolate import PchipInterpolator
from scipy.optimize import least_squares, lsq_linear

from hogline.adapters.sources import digest
from hogline.config import Settings
from hogline.db import Database
from hogline.domain.catalog import source_or_error
from hogline.domain.skill_estimates import estimate_report
from hogline.media.frames import capture, resolve_media
from hogline.media.windows import decode_window
from hogline.perception.delivery_ray import ice_points, image_points
from hogline.perception.perspective_rays import _matrix, project
from hogline.perception.stones import RawHSVHandleDetector

VERSION = "one-shot-planar-replay-v1"
GAME = "ae81313f-9cc1-549f-b5c6-27f9b416d8a0"
CANDIDATE = "ac2a4232-fe05-5529-a6b4-537a2b47d03a"
START, END = 2118.0, 2158.0
ROOT = Path("data/shot-reconstruction/example")
BOUNDS = {
    "house_bottom": [480, 400, 800, 720],
    "house_top": [480, 0, 800, 320],
    "perspective_left": [0, 0, 480, 684],
    "perspective_right": [800, 0, 1280, 684],
}
# Assistant-inspected camera-near bottom granite edges, crop-local pixels.
# Shared timestamps mean shared broadcast PTS, not proven synchronized exposures.
REVIEWED = {
    "perspective_right": [
        [2121.5, 227, 297],
        [2122, 228, 299],
        [2122.5, 216, 302],
        [2124, 190, 333],
        [2126, 163, 372],
        [2128, 135, 417],
        [2130, 110, 471],
        [2132, 85, 532],
        [2134, 64, 608],
    ],
    "perspective_left": [
        [2132, 380, 585],
        [2134, 365, 513],
        [2136, 349, 462],
        [2138, 334, 422],
        [2140, 318, 394],
    ],
}


def write_json(name, value):
    (ROOT / name).write_text(json.dumps(value, indent=2))


def inputs():
    db = Database(Settings.load())
    report = estimate_report(db, GAME)
    if report["stale"]:
        raise ValueError("The reviewed example report is stale")
    shot = next(d for p in report["players"] for d in p["deliveries"] if d["candidate_id"] == CANDIDATE)
    ray = shot["delivery_ray"]
    if not ray or not ray.get("perspectives"):
        raise ValueError("Verified native and perspective rays are required")
    with db.snapshot_session() as session:
        source = source_or_error(session, ray["source_id"])
    handle = resolve_media(source)
    if handle["sha256"] != ray["media_sha256"]:
        raise ValueError("Original source media changed")
    ROOT.mkdir(parents=True, exist_ok=True)
    return report, ray, source, handle


def register_cameras(ray):
    """Register existing local maps using other reviewed shots and ASSUMED length.

    The existing independent distance check stays unchanged. Here 114 ft fixes the
    world scale; subpixel local-map uncertainty otherwise leaves a gap at mid-sheet.
    """
    cameras = ray["perspective_calibration"]["cameras"]
    pairs = ray["perspective_calibration"]["pairs"]
    views = list(cameras)
    initial = []
    for v in views:
        h = np.array(cameras[v]["local_to_pixel"])
        x, y = h[:2, 2]
        g, k = h[2, :2]
        initial.extend([x, y, h[0, 0] - x * g, h[0, 1] - x * k, h[1, 0] - y * g, h[1, 1] - y * k, g, k])
    initial = np.array(initial)
    landmarks = np.array([[x, y] for y in [-1.8288, 0, 1.8288, 6.4008] for x in [-1.8288, 0, 1.8288]])
    reference = {v: project(landmarks, cameras[v]["local_to_pixel"]) for v in views}

    def residual(p):
        maps = {v: _matrix(p[i * 8 : (i + 1) * 8]) for i, v in enumerate(views)}
        result = []
        for v in views:
            result.extend(((project(landmarks, maps[v]) - reference[v]) / 0.8).ravel())
        for pair in pairs:
            q = {v: project([pair[v]], np.linalg.inv(maps[v]))[0] for v in views}
            result.append((sum(a[1] for a in q.values()) - 0.29 - ray["nominal_tee_distance_m"]) / 0.15)
            result.append(
                (
                    q[views[0]][0] * cameras[views[0]]["transverse_sign"]
                    - q[views[1]][0] * cameras[views[1]]["transverse_sign"]
                )
                / 0.05
            )
        return result

    delta = np.array([2, 2, 3, 1, 1, 1, 0.004, 0.008] * 2)
    fit = least_squares(
        residual, initial, bounds=(initial - delta, initial + delta), loss="soft_l1", max_nfev=250
    )
    if not fit.success:
        raise ValueError("Common-sheet camera registration did not converge")
    result = {}
    for i, v in enumerate(views):
        h = _matrix(fit.x[i * 8 : (i + 1) * 8])
        result[v] = {**cameras[v], "local_to_pixel": h.tolist(), "pixel_to_local": np.linalg.inv(h).tolist()}
    return result


def ground_proposal(rgb, expected):
    """A yellow handle plus a separate gray body; reject merged people/brushes."""
    ex, ey = expected
    x0, x1 = max(0, round(ex) - 35), min(rgb.shape[1], round(ex) + 36)
    y0, y1 = max(0, round(ey) - 42), min(rgb.shape[0], round(ey) + 12)
    hsv = cv2.cvtColor(rgb[y0:y1, x0:x1], cv2.COLOR_RGB2HSV)
    mask = cv2.inRange(hsv, (17, 65, 100), (40, 255, 255))
    count, labels, stats, centers = cv2.connectedComponentsWithStats(mask)
    choices = []
    for k in range(1, count):
        x, y, w, h, area = stats[k]
        cx, cy = centers[k] + [x0, y0]
        if not (3 <= area <= 350 and 2 <= w <= 35 and 1 <= h <= 20):
            continue
        if not (3 <= ey - cy <= 35 and abs(cx - ex) < 15):
            continue
        gray = cv2.inRange(hsv, (0, 0, 60), (179, 85, 180))
        gray[: max(0, round(cy - y0) + 2)] = 0
        gray = cv2.morphologyEx(gray, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
        n, _, boxes, _ = cv2.connectedComponentsWithStats(gray)
        for j in range(1, n):
            bx, by, bw, bh, body_area = boxes[j]
            bottom = y0 + by + bh - 0.5
            middle = x0 + bx + bw / 2
            if not (8 <= bw <= 65 and 3 <= bh <= 35 and body_area >= 25):
                continue
            if not (bx <= cx - x0 <= bx + bw and by <= cy - y0 + 14):
                continue
            if abs(bottom - ey) > 6 or abs(middle - ex) > 8:
                continue
            choices.append(
                (abs(bottom - ey) + abs(middle - ex), [float(middle), float(bottom)], [float(cx), float(cy)])
            )
    return min(choices, default=None, key=lambda q: q[0])


def to_world(pixel, view, ray, cameras, *, ground_edge=False):
    if view in cameras:
        c = cameras[view]
        q = project([pixel], c["pixel_to_local"])[0]
        d = q[1] - (0.145 if ground_edge else 0)
        s = d if c["house_view"] == ray["origin_view"] else ray["nominal_tee_distance_m"] - d
        return [float(c["transverse_sign"] * q[0]), float(s)]
    geometry = ray["geometry"] if view == ray["origin_view"] else ray["receiving_geometry"]
    q = ice_points([pixel], geometry)[0]
    s = ray["forward_sign"] * q[1]
    if view != ray["origin_view"]:
        s += ray["nominal_tee_distance_m"]
    return [float(q[0]), float(s)]


def fit_track(observations):
    """Whole-shot least-squares smoother, constrained to forward travel and rest.

    Input error scales and smoothing strengths are assumptions for this example;
    they are not calibrated probabilities or physical-accuracy measurements.
    """
    grid = np.arange(START, END + 0.01, 0.25)
    n = len(grid)
    rows, targets, scales = [], [], []
    for o in observations:
        index = min(n - 2, max(0, int((o["t"] - START) / 0.25)))
        fraction = (o["t"] - grid[index]) / 0.25
        row = np.zeros(n)
        row[index], row[index + 1] = 1 - fraction, fraction
        rows.append(row)
        targets.append(o["world_m"])
        scales.append(o["fit_error_scale_m"])
    a = np.array(rows)
    targets, scales = np.array(targets), np.array(scales)
    d2 = np.diff(np.eye(n), n=2, axis=0) / 0.25**2
    d3 = np.diff(np.eye(n), n=3, axis=0) / 0.25**3
    first = min(observations, key=lambda o: o["t"])["world_m"]
    final = np.median(
        [o["world_m"] for o in observations if o["view"] == "house_top" and o["t"] >= 2152], axis=0
    )
    if not np.isfinite(final).all():
        raise ValueError("Stationary receiving-house observations are required")
    stationary = [(i, first) for i, t in enumerate(grid) if t < 2121.5]
    stationary += [(i, final) for i, t in enumerate(grid) if t >= 2151.5]
    rest = np.eye(n)[[i for i, _ in stationary]]
    rest_targets = np.array([p for _, p in stationary])
    output = []
    for axis in range(2):
        w = 1 / scales[:, axis]
        # Permit the drive acceleration; encourage smoother motion after delivery.
        acceleration_scale = 0.12 if axis == 0 else 0.5
        regularizer = d2 / acceleration_scale
        regularizer[grid[1:-1] < 2124] *= 0.08
        system = np.vstack((a * w[:, None], regularizer, d3 * (0.05 if axis else 0.15), rest / 0.05))
        rhs = np.concatenate(
            (targets[:, axis] * w, np.zeros(len(d2) + len(d3)), rest_targets[:, axis] / 0.05)
        )
        if axis == 0:
            solution = np.linalg.lstsq(system, rhs, rcond=None)[0]
        else:
            # Variables are starting S and nonnegative per-frame forward increments.
            cumulative = np.tril(np.ones((n, n)))
            lower, upper = np.zeros(n), np.full(n, 0.25 * 4)
            lower[0], upper[0] = -5, 0
            fitted = lsq_linear(system @ cumulative, rhs, bounds=(lower, upper), tol=1e-9)
            if not fitted.success:
                raise ValueError("Forward-motion smoother did not converge")
            solution = cumulative @ fitted.x
        output.append(solution)
    track = np.column_stack((grid, *output))
    residual = a @ track[:, 1:] - targets
    return track, {
        "median_anchor_residual_m": np.median(np.abs(residual), axis=0).tolist(),
        "maximum_anchor_residual_m": np.max(np.abs(residual), axis=0).tolist(),
        "method": "whole-shot weighted position factors, acceleration/jerk regularization, "
        "nonnegative forward increments, setup/rest anchors; future observations constrain gaps",
        "calibrated_probability": False,
        "final_position_m": final.tolist(),
    }


def prepare():
    report, ray, source, handle = inputs()
    cameras = register_cameras(ray)
    observations = []
    for p in ray["points"]:
        observations.append(
            {
                "t": p["t"],
                "pts": p["pts"],
                "time_base_num": p["time_base_num"],
                "time_base_den": p["time_base_den"],
                "view": ray["origin_view"],
                "pixel": p["pixel"],
                "world_m": to_world(p["pixel"], ray["origin_view"], ray, cameras),
                "fit_error_scale_m": [0.04, 0.10],
                "basis": "existing_native_handle_track",
                "crop_sha256": p["crop_sha256"],
            }
        )
    for view, points in REVIEWED.items():
        for t, x, y in points:
            im, timing = capture(handle["uri"], t - 0.00001)
            actual = timing["pts"] * timing["time_base_num"] / timing["time_base_den"]
            if abs(actual - t) > 1 / 60 + 1e-6:
                raise ValueError("Reviewed granite point no longer matches its source frame")
            observations.append(
                {
                    "t": actual,
                    **timing,
                    "view": view,
                    "pixel": [x, y],
                    "world_m": to_world([x, y], view, ray, cameras, ground_edge=True),
                    "fit_error_scale_m": [0.15, 0.45],
                    "basis": "assistant_reviewed_bottom_granite_edge",
                    "crop_sha256": hashlib.sha256(im.crop(BOUNDS[view]).tobytes()).hexdigest(),
                }
            )
    interpolators = {v: PchipInterpolator(np.array(p)[:, 0], np.array(p)[:, 1:]) for v, p in REVIEWED.items()}
    review_tiles = []
    detector = RawHSVHandleDetector()
    sample_count = 0
    for lo, hi in ((START, START + 20), (START + 20, END)):
        for im, timing in decode_window(handle["uri"], lo, hi, interval=0.125, max_frames=160):
            t = timing["pts"] * timing["time_base_num"] / timing["time_base_den"]
            added = []
            for view, inter in interpolators.items():
                lower = 2124.15 if view.endswith("right") else 2131.5
                upper = 2134.5 if view.endswith("right") else 2140.5
                if not lower <= t <= upper:
                    continue
                crop = im.crop(BOUNDS[view])
                proposal = ground_proposal(np.asarray(crop), inter(t))
                if proposal is None:
                    continue
                _, pixel, handle_pixel = proposal
                observations.append(
                    {
                        "t": t,
                        **timing,
                        "view": view,
                        "pixel": pixel,
                        "handle_pixel": handle_pixel,
                        "world_m": to_world(pixel, view, ray, cameras, ground_edge=True),
                        "fit_error_scale_m": [0.16, 0.5],
                        "basis": "yellow_handle_with_gray_granite_body",
                        "crop_sha256": hashlib.sha256(crop.tobytes()).hexdigest(),
                    }
                )
                added.append((view, pixel))
            if 2147 <= t:
                crop = im.crop(BOUNDS["house_top"])
                found = [
                    d
                    for d in detector.detect(np.asarray(crop))
                    if d["color"] == "yellow"
                    and 160 < d["center_px"][0] < 220
                    and 220 < d["center_px"][1] < 307
                    and 30 <= d["area_px2"] <= 550
                ]
                if len(found) == 1:
                    pixel = found[0]["center_px"]
                    observations.append(
                        {
                            "t": t,
                            **timing,
                            "view": "house_top",
                            "pixel": pixel,
                            "world_m": to_world(pixel, "house_top", ray, cameras),
                            "fit_error_scale_m": [0.045, 0.08],
                            "basis": "isolated_receiving_house_handle",
                            "crop_sha256": hashlib.sha256(crop.tobytes()).hexdigest(),
                        }
                    )
                    added.append(("house_top", pixel))
            if sample_count % 8 == 0:
                tile = im.copy()
                draw = ImageDraw.Draw(tile)
                for view, pixel in added:
                    x, y = np.array(pixel) + BOUNDS[view][:2]
                    draw.ellipse((x - 14, y - 14, x + 14, y + 14), outline="magenta", width=3)
                tile = tile.resize((640, 360))
                ImageDraw.Draw(tile).text(
                    (5, 95), f"{t:.3f} s", fill="magenta", stroke_width=1, stroke_fill="white"
                )
                review_tiles.append(tile)
            sample_count += 1
        print(f"Decoded original PTS through {hi:g} s", flush=True)
    observations.sort(key=lambda o: (o["t"], o["view"]))
    track, fit = fit_track(observations)
    atlas = Image.new("RGB", (1280, 360 * ((len(review_tiles) + 1) // 2)), "white")
    for i, tile in enumerate(review_tiles):
        atlas.paste(tile, ((i % 2) * 640, (i // 2) * 360))
    atlas.save(ROOT / "tracking-review.jpg")
    write_json("observations.json", observations)
    write_json(
        "trajectory.json",
        {
            "version": VERSION,
            "grid_columns": ["source_seconds", "transverse_m", "forward_m"],
            "grid": track.tolist(),
            "fit": fit,
            "observation_times": sorted(set(o["t"] for o in observations)),
            "notice": "Approximate 2D reconstruction. Camera-supported anchors and inferred gaps "
            "are distinct. No target, release accuracy, calibrated uncertainty or stone identity accepted.",
        },
    )
    write_json(
        "geometry.json",
        {
            "cameras": cameras,
            "origin": ray["geometry"],
            "receiving": ray["receiving_geometry"],
            "tee_distance_m": ray["nominal_tee_distance_m"],
        },
    )
    write_json(
        "manifest.json",
        {
            "version": VERSION,
            "game_id": GAME,
            "candidate_id": CANDIDATE,
            "label": "Lindsey · end 3 · first yellow delivery",
            "source_id": source["id"],
            "source_metadata_sha256": source["metadata_sha256"],
            "media_sha256": handle["sha256"],
            "review_sha256": report["review_sha256"],
            "ray_id": ray["id"],
            "ray_sha256": digest(ray),
            "clip_start_seconds": START,
            "clip_end_seconds": END,
            "sampled_original_frames": sample_count,
            "nominal_tee_distance_assumed_m": ray["nominal_tee_distance_m"],
            "registration": "Other-shot paired granite observations align local camera maps to "
            "ASSUMED 114 ft. This does not independently measure button distance.",
            "limitations": [
                "flat ice only; people and raised stones distort when warped",
                "camera exposures not proven synchronous; lens distortion uncalibrated",
                "granite-edge and handle centres are approximate position cues",
                "empty reference ice fills unseen pixels; no hidden activity is recovered",
                "gap path is a smooth constrained estimate using earlier and later observations",
            ],
        },
    )
    # Accurate-seek transcode from the verified parent; original has video only.
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            str(START),
            "-i",
            handle["uri"],
            "-t",
            str(END - START),
            "-map",
            "0:v:0",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(ROOT / "source-shot.mp4"),
        ],
        check=True,
    )
    print(f"Prepared {len(observations)} position observations; fit {fit}", flush=True)


@functools.lru_cache(maxsize=16)
def font(size, bold=False):
    name = "Arial Bold.ttf" if bold else "Arial.ttf"
    path = Path("/System/Library/Fonts/Supplemental") / name
    return ImageFont.truetype(str(path), size) if path.exists() else ImageFont.load_default(size=size)


TEAL, AMBER, WHITE, MUTED = "#25d3b0", "#ffbf5a", "#f2f7fa", "#adc1ca"
PANE_SIZE = (220, 855)
SCALE = 20.0


def plane_pixels(world, distance):
    world = np.asarray(world)
    return np.column_stack((PANE_SIZE[0] / 2 + world[:, 0] * SCALE, (distance + 4 - world[:, 1]) * SCALE))


def plane_maps(geometry):
    width, height = PANE_SIZE
    yy, xx = np.mgrid[:height, :width]
    world = np.column_stack(
        ((xx.ravel() - width / 2) / SCALE, geometry["tee_distance_m"] + 4 - yy.ravel() / SCALE)
    )
    maps = {}
    for view in BOUNDS:
        w, h = BOUNDS[view][2] - BOUNDS[view][0], BOUNDS[view][3] - BOUNDS[view][1]
        if view in geometry["cameras"]:
            camera = geometry["cameras"][view]
            local = world.copy()
            local[:, 0] *= camera["transverse_sign"]
            if camera["house_view"] == "house_top":
                local[:, 1] = geometry["tee_distance_m"] - world[:, 1]
            matrix = np.asarray(camera["local_to_pixel"])
            q = np.column_stack((local, np.ones(len(local)))) @ matrix.T
            valid = q[:, 2] > 1e-6
            p = q[:, :2] / np.maximum(q[:, 2:], 1e-6)
            weight = np.clip((22 - local[:, 1]) / 8, 0, 1)
            valid &= (p[:, 1] > camera["tee_px"][1] - 40) & (p[:, 1] < camera["ice_bottom_px"])
        else:
            local = world.copy()
            local[:, 1] = -world[:, 1] if view == "house_bottom" else geometry["tee_distance_m"] - world[:, 1]
            g = geometry["origin"] if view == "house_bottom" else geometry["receiving"]
            p = image_points(local, g)
            valid = np.ones(len(world), dtype=bool)
            weight = 8 * np.clip((2.1 - np.abs(local[:, 1])) / 0.3, 0, 1)
            valid &= (p[:, 0] >= 12) & (p[:, 0] < w - 12) & (p[:, 1] >= 12) & (p[:, 1] < h - 12)
        valid &= (p[:, 0] >= 2) & (p[:, 0] < w - 2) & (p[:, 1] >= 2) & (p[:, 1] < h - 2)
        valid &= np.abs(world[:, 0]) < 2.375
        maps[view] = (
            p[:, 0].reshape(height, width).astype(np.float32),
            p[:, 1].reshape(height, width).astype(np.float32),
            (weight * valid).reshape(height, width),
        )
    return maps


def warp(frame, maps, reference=None):
    height, width = PANE_SIZE[1], PANE_SIZE[0]
    total = np.zeros((height, width, 3), dtype=float)
    weight = np.zeros((height, width))
    for view, (mx, my, w) in maps.items():
        x0, y0, x1, y1 = BOUNDS[view]
        crop = frame[y0:y1, x0:x1]
        pixels = cv2.remap(crop, mx, my, cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT)
        total += pixels * w[:, :, None]
        weight += w
    valid = weight > 0
    image = np.full((height, width, 3), (22, 48, 61), dtype=np.uint8)
    if reference is not None:
        image[:] = np.asarray(reference)
    image[valid] = np.clip(total[valid] / weight[valid, None], 0, 255).astype(np.uint8)
    return Image.fromarray(image)


def vector_ice(distance):
    image = Image.new("RGB", PANE_SIZE, "#16303d")
    draw = ImageDraw.Draw(image)
    x0, x1 = plane_pixels([[-2.375, 0], [2.375, 0]], distance)[:, 0]
    draw.rectangle((x0, 0, x1, PANE_SIZE[1]), fill="#edf3f3")
    for s in (0, distance):
        cx, cy = plane_pixels([[0, s]], distance)[0]
        for radius, color in (
            (1.8288, "#60a9cb"),
            (1.2192, "#edf3f3"),
            (0.6096, "#a9ca80"),
            (0.1524, "#edf3f3"),
        ):
            r = radius * SCALE
            draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=color)
        draw.line((x0, cy, x1, cy), fill="#9cb4bd", width=1)
    for s in (6.4008, distance - 6.4008):
        y = plane_pixels([[0, s]], distance)[0, 1]
        draw.line((x0, y, x1, y), fill="#d36b75", width=2)
    center = plane_pixels([[0, -4], [0, distance + 4]], distance)
    draw.line([tuple(p) for p in center], fill="#b3c4c8", width=1)
    return image


def draw_track(draw, track, t, observation_times, convert, *, width=3, future=True):
    pixels = convert(track[:, 1:])
    for i in range(1, len(track)):
        past = track[i, 0] <= t
        mid = (track[i, 0] + track[i - 1, 0]) / 2
        supported = np.min(np.abs(observation_times - mid)) <= 0.3
        if past:
            color = TEAL if supported else AMBER
            if not supported and i % 2:
                continue
        elif future:
            color = "#9daeb8"
        else:
            continue
        draw.line([tuple(pixels[i - 1]), tuple(pixels[i])], fill=color, width=width if past else 1)


def render():
    report, ray, _, handle = inputs()
    manifest = json.loads((ROOT / "manifest.json").read_text())
    if manifest["media_sha256"] != handle["sha256"] or manifest["review_sha256"] != report["review_sha256"]:
        raise ValueError("Rebuild the example after its source/review changes")
    trajectory = json.loads((ROOT / "trajectory.json").read_text())
    observations = json.loads((ROOT / "observations.json").read_text())
    geometry = json.loads((ROOT / "geometry.json").read_text())
    distance = geometry["tee_distance_m"]
    track = np.array(trajectory["grid"])
    observation_times = np.array(trajectory["observation_times"])
    interpolator = PchipInterpolator(track[:, 0], track[:, 1:])
    maps = plane_maps(geometry)
    reference, _ = capture(handle["uri"], 300)
    reference_warp = warp(np.asarray(reference), maps)
    reference_warp.save(ROOT / "reference-warp.png")
    ice = vector_ice(distance)
    width, height, fps = 1600, 1080, 24
    static = Image.new("RGB", (width, height), "#0c202c")
    draw = ImageDraw.Draw(static)
    draw.text((28, 22), "ONE SHOT, FROM DELIVERY TO REST", font=font(31, True), fill=WHITE)
    draw.text(
        (28, 64),
        "Lindsey · end 3 · original footage + approximate top-down reconstruction",
        font=font(19),
        fill=MUTED,
    )
    draw.text((28, 106), "ORIGINAL CAMERA COMPOSITE", font=font(17, True), fill=WHITE)
    draw.text((1040, 106), "WARPED VIDEO", font=font(17, True), fill=WHITE)
    draw.text((1320, 106), "STONE PATH", font=font(17, True), fill=WHITE)
    draw.text((1040, 133), "Four cameras / flat ice", font=font(13), fill=MUTED)
    draw.text((1320, 133), "Before + after gap fitting", font=font(13), fill=MUTED)
    draw.text(
        (28, 764),
        "FULL PATH · TEAL: CAMERA-SUPPORTED · AMBER DASHES: UNMEASURED / OCCLUDED",
        font=font(15, True),
        fill=WHITE,
    )
    draw.text(
        (28, 1048),
        "Assumed button spacing: 114 ft. Raised people distort in the video warp. "
        "Hidden motion is estimated, not recovered pixels.",
        font=font(14),
        fill=MUTED,
    )
    draw.text((1040, 1048), "Same nominal sheet scale", font=font(13), fill=MUTED)
    # Original camera presentation frames are only sampled for this 24 fps export.
    # The source clip stays 60 Hz; all measurements retain original 90 kHz PTS.
    decoder = subprocess.Popen(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-threads",
            "2",
            "-i",
            str(ROOT / "source-shot.mp4"),
            "-vf",
            f"fps={fps}",
            "-pix_fmt",
            "rgb24",
            "-f",
            "rawvideo",
            "pipe:1",
        ],
        stdout=subprocess.PIPE,
    )
    encoder = subprocess.Popen(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-s",
            f"{width}x{height}",
            "-r",
            str(fps),
            "-i",
            "pipe:0",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(ROOT / "reconstruction.mp4"),
        ],
        stdin=subprocess.PIPE,
    )
    preview_frames = []
    try:
        for i in range(round((END - START) * fps)):
            raw = decoder.stdout.read(1280 * 720 * 3)
            if len(raw) != 1280 * 720 * 3:
                raise ValueError("Source clip ended before the complete-shot replay")
            t = START + i / fps
            original = np.frombuffer(raw, dtype=np.uint8).reshape(720, 1280, 3)
            frame = static.copy()
            source_image = Image.fromarray(original).resize((960, 540), Image.Resampling.LANCZOS)
            frame.paste(source_image, (28, 138))
            mosaic = warp(original, maps, reference_warp)
            clean = ice.copy()
            world = interpolator(t)

            def convert(points):
                return plane_pixels(points, distance)

            for panel in (mosaic, clean):
                painter = ImageDraw.Draw(panel)
                draw_track(painter, track, t, observation_times, convert)
                x, y = convert([world])[0]
                supported = np.min(np.abs(observation_times - t)) <= 0.3
                painter.ellipse((x - 7, y - 7, x + 7, y + 7), fill="#ffd34c", outline="#102733", width=2)
                painter.ellipse(
                    (x - 10, y - 10, x + 10, y + 10), outline=TEAL if supported else AMBER, width=2
                )
            frame.paste(mosaic, (1040, 158))
            frame.paste(clean, (1320, 158))
            painter = ImageDraw.Draw(frame)
            if t < 2121.5:
                phase = "SETUP"
            elif t >= 2151.5:
                phase = "SETTLED · FINAL POSITION OBSERVED"
            elif np.min(np.abs(observation_times - t)) > 0.3:
                phase = "VISIBILITY / MEASUREMENT GAP · PATH ESTIMATED"
            else:
                phase = "CAMERA-SUPPORTED TRACK"
            painter.text((28, 700), f"{t - START:05.2f} s / 40.00 s", font=font(25, True), fill=WHITE)
            painter.text((325, 706), phase, font=font(17, True), fill=AMBER if "GAP" in phase else TEAL)
            painter.text(
                (28, 738),
                f"Source {int(t // 60):02d}:{t % 60:05.2f} · "
                f"Position: {world[1]:.2f} m along sheet / {world[0]:+.2f} m across",
                font=font(16),
                fill=MUTED,
            )
            # Rotate the clean sheet for a large, geometrically faithful full-path overview.
            # Crop unused side gutters after rotation, preserving uniform scale.
            overview = ice.crop((56, 0, 164, 855)).rotate(90, expand=True).resize((941, 119))
            overview_draw = ImageDraw.Draw(overview)

            def horizontal(points):
                p = plane_pixels(points, distance)
                return np.column_stack(((PANE_SIZE[1] - 1 - p[:, 1]) * 941 / 855, (p[:, 0] - 56) * 119 / 108))

            draw_track(overview_draw, track, t, observation_times, horizontal, width=4)
            x, y = horizontal([world])[0]
            overview_draw.ellipse((x - 6, y - 6, x + 6, y + 6), fill="#ffd34c", outline="#102733", width=2)
            frame.paste(overview, (28, 804))
            painter = ImageDraw.Draw(frame)
            painter.text((28, 931), "DELIVERY END", font=font(13, True), fill=MUTED)
            painter.text((822, 931), "RECEIVING END", font=font(13, True), fill=MUTED)
            encoder.stdin.write(frame.tobytes())
            if i % (fps * 5) == 0:
                frame.save(ROOT / f"preview-{i // fps:02d}.png")
                preview_frames.append(frame.resize((800, 500)))
                print(f"Rendered {i // fps} / 40 s", flush=True)
            if i == fps * 38:
                frame.save(ROOT / "poster.png")
    finally:
        decoder.stdout.close()
        encoder.stdin.close()
        decoder.wait()
        encoder.wait()
    if decoder.returncode or encoder.returncode:
        raise ValueError("Video decoder/encoder failed")
    sheet = Image.new("RGB", (1600, 500 * ((len(preview_frames) + 1) // 2)), "white")
    for i, f in enumerate(preview_frames):
        sheet.paste(f, ((i % 2) * 800, (i // 2) * 500))
    sheet.save(ROOT / "render-review.jpg")
    manifest["artifacts"] = {}
    for name in (
        "source-shot.mp4",
        "reconstruction.mp4",
        "trajectory.json",
        "observations.json",
        "poster.png",
    ):
        path = ROOT / name
        with path.open("rb") as f:
            sha = hashlib.file_digest(f, "sha256").hexdigest()
        manifest["artifacts"][name] = {"bytes": path.stat().st_size, "sha256": sha}
    manifest["render_fps"] = fps
    manifest["position_observations"] = len(observations)
    manifest["largest_unmeasured_gap_seconds"] = float(np.max(np.diff(observation_times)))
    write_json("manifest.json", manifest)
    make_viewer(manifest)


def make_viewer(manifest):
    html = """<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>One shot · top-down reconstruction</title><style>
body{margin:0;background:#0c202c;color:#edf5f7;font:17px/1.55 system-ui,sans-serif}
main{max-width:1280px;margin:auto;padding:30px}h1{font-size:32px;line-height:1.15;margin-bottom:10px}
p{max-width:1000px;color:#b6cbd3}a{color:#5ce4ca}video{display:block;width:100%;background:#142d3a;border-radius:10px}
.controls{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin:14px 0 30px}
button,select{font:inherit;background:#193b4b;color:white;border:1px solid #537380;
padding:8px 12px;border-radius:6px}
details{margin-top:25px}summary{cursor:pointer}small{color:#b6cbd3}.key{display:inline-block;margin-right:20px}
</style><main><h1>One shot, from delivery to rest</h1>
<p>Lindsey · end 3 · first yellow delivery. Original recording 35:18–35:58.
The warped video flattens four camera views onto the ice.
The clean replay estimates the stone's full path.</p>
<video id="replay" controls playsinline preload="metadata"
poster="poster.png" src="reconstruction.mp4"></video>
<div class="controls"><button id="restart">Replay shot</button>
<label>Speed <select id="speed"><option value="0.5">½×</option><option value="1" selected>1×</option>
<option value="2">2×</option></select></label><a href="reconstruction.mp4" download>Download replay</a>
<a href="source-shot.mp4" download>Download original clip</a></div>
<p><span class="key" style="color:#25d3b0">● Teal: camera-supported positions</span>
<span class="key" style="color:#ffbf5a">┄ Amber: unmeasured / occluded path</span></p>
<p>The long visibility gap lasts about 8.6 seconds. Earlier motion and the later observed arrival constrain
the estimate through it. The yellow marker is reconstructed;
it is not a recovered image of the hidden stone.</p>
<details><summary>Original clip and reconstruction limits</summary>
<p>Setup through the observed resting stone, with original 60 Hz source presentation frames.</p>
<video controls playsinline preload="none" src="source-shot.mp4"></video>
<p>Approximate flat-ice geometry, assuming 114 ft / 34.7472 m between buttons. Registration uses paired
stones from other shots. This does not independently measure the sheet length. Raised people stretch in
the warp; empty reference ice is only a background fill. Camera exposure timing and lens distortion remain
uncalibrated. The smooth gap path is one plausible estimate, not a uniquely recovered trajectory.</p>
<p><a href="trajectory.json">Trajectory and fit details</a> ·
<a href="observations.json">Timestamped observations</a> ·
<a href="manifest.json">Source checksums and method</a></p></details></main>
<script>const v=document.getElementById('replay');
document.getElementById('restart').onclick=()=>{v.currentTime=0;v.play()};
document.getElementById('speed').onchange=e=>v.playbackRate=Number(e.target.value);</script></html>"""
    (ROOT / "index.html").write_text(html)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=["prepare", "render"])
    args = parser.parse_args()
    if args.phase == "prepare":
        prepare()
    else:
        render()
