"""Bounded native hog bursts, reusing the bird's-eye reconstruction camera registration."""

import hashlib
import json

import cv2
import numpy as np

from hogline.adapters.sources import digest
from hogline.domain.catalog import source_or_error
from hogline.media.frames import capture, resolve_media
from hogline.media.windows import decode_native_crop
from hogline.orchestration.throw_reconstructions import atomic_json
from hogline.perception.hog_ray import VERSION, fit_hog_ray
from hogline.perception.perspective_rays import HOG_FROM_TEE_M, register_common_sheet
from hogline.perception.throw_reconstruction import BOUNDS, ground_candidates, orientation, world


def key(ray):
    return digest({"version": VERSION, "ray": ray})


def saved(db, ray):
    path = db.settings.data_dir / "hog-rays" / (key(ray) + ".json")
    if not path.is_file():
        return None
    try:
        result = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    checksum = result.pop("sha256", None)
    if checksum != digest(result):
        return None
    if result.get("ray_sha256") != digest(ray):
        return None
    if result.get("status") == "unavailable":
        return result
    image = path.with_suffix(".png")
    if not image.is_file() or hashlib.sha256(image.read_bytes()).hexdigest() != result.get("image_sha256"):
        return None
    return result if result.get("ray_sha256") == digest(ray) else None


def measure(db, ray):
    try:
        return _measure(db, ray)
    except ValueError as exc:
        result = {"status": "unavailable", "error": str(exc), "ray_sha256": digest(ray), "version": VERSION}
        root = db.settings.data_dir / "hog-rays"
        root.mkdir(exist_ok=True)
        atomic_json(root / (key(ray) + ".json"), {**result, "sha256": digest(result)})
        raise


def _measure(db, ray):
    if (result := saved(db, ray)) and result["status"] != "unavailable":
        return result
    cameras = register_common_sheet(ray)
    view = next(v for v, c in cameras.items() if c["house_view"] == ray["origin_view"])
    native = ray["points"][-24:]
    times = np.array([p["t"] for p in native])
    s = np.array([ray["forward_sign"] * p["ice_m"][1] for p in native])
    speed, position = np.polyfit(times - times[0], s, 1)
    if not 0.5 < speed < 6:
        raise ValueError("Delivery track cannot locate a bounded hog-line search")
    guess = float(times[0] + (HOG_FROM_TEE_M - position) / speed)
    with db.snapshot_session() as session:
        source = source_or_error(session, ray["source_id"])
    handle = resolve_media(source)
    if handle["sha256"] != ray["media_sha256"]:
        raise ValueError("Source media changed")
    rows, offsets = [], []
    for im, tm in decode_native_crop(handle["uri"], max(0, guess - 1.5), guess + 1.5, BOUNDS[view]):
        rgb = np.asarray(im)
        mask = cv2.inRange(cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV), (17, 65, 100), (40, 255, 255))
        n, _, boxes, centers = cv2.connectedComponentsWithStats(mask)
        t = tm["pts"] * tm["time_base_num"] / tm["time_base_den"]
        expected_s = HOG_FROM_TEE_M + speed * (t - guess)
        expected_x = orientation(ray) * (
            ray["near_tee_x_m"] + ray["slope_transverse_per_forward"] * expected_s
        )
        options = []
        for i in range(1, n):
            _, _, w, h, area = boxes[i]
            x, y = centers[i]
            if not (3 <= area <= 400 and 2 <= w <= 35 and 1 <= h <= 20 and 300 < y < 490):
                continue
            q = world([x, y + 12], view, ray, cameras, ground=True)
            if abs(q[1] - expected_s) < 1.4 and abs(q[0] - expected_x) < 0.8:
                options.append((abs(q[0] - expected_x) + abs(q[1] - expected_s) * 0.3, [float(x), float(y)]))
        if not options:
            continue
        options.sort()
        if len(options) > 1 and options[1][0] - options[0][0] < 0.08:
            continue
        pixel = options[0][1]
        for ground in ground_candidates(rgb):
            delta = np.array(ground) - pixel
            if abs(delta[0]) < 5 and 5 < delta[1] < 25:
                offsets.append(delta.tolist())
        rows.append({"t": t, **tm, "pixel": pixel, "crop_sha256": hashlib.sha256(rgb.tobytes()).hexdigest()})
    if len(offsets) < 8:
        raise ValueError("Too few matching granite edges to anchor handle height near the hog")
    offset = np.median(offsets, axis=0)
    for row in rows:
        row["world_m"] = world((np.array(row["pixel"]) + offset).tolist(), view, ray, cameras, ground=True)
    result = fit_hog_ray(rows, ray["nominal_tee_distance_m"])
    result.update(
        ray_sha256=digest(ray), camera=view, pixel_offset=offset.tolist(), anchor_count=len(offsets)
    )
    root = db.settings.data_dir / "hog-rays"
    root.mkdir(exist_ok=True)
    nearest = min(result["points"], key=lambda p: abs(p["t"] - result["crossing_seconds"]))
    image, timing = capture(handle["uri"], nearest["t"] - 0.00001)
    crop = image.crop(BOUNDS[view])
    if (
        timing["pts"] != nearest["pts"]
        or hashlib.sha256(np.asarray(crop).tobytes()).hexdigest() != nearest["crop_sha256"]
    ):
        raise ValueError("Hog-line preview disagrees with its native observation")
    image_path = root / (key(ray) + ".png")
    crop.save(image_path)
    result.update(
        image_sha256=hashlib.sha256(image_path.read_bytes()).hexdigest(), preview_seconds=nearest["t"]
    )
    atomic_json(root / (key(ray) + ".json"), {**result, "sha256": digest(result)})
    return result
