"""Image-space obstruction cues, never a person detector or clear-ice certificate."""

import cv2
import numpy as np

from hogline.perception.house import house_ring

VERSION = "dark-and-blue-ring-obstruction-v2"


def large_regions(mask, kind):
    height, width = mask.shape
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    count, _, stats, _ = cv2.connectedComponentsWithStats(mask)
    return [
        {"bbox_px": [int(x), int(y), int(w), int(h)], "area_px2": int(area), "kind": kind}
        for x, y, w, h, area in stats[1:count]
        if area >= width * height * 0.01 and max(w / width, h / height) >= 0.15
    ]


def visibility_observation(rgb):
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    # Low-value, modest-saturation regions avoid blue/green painted rings.
    # Gray clothing can be too bright for this test, so inspect ring occlusions too.
    dark = cv2.inRange(hsv, (0, 0, 0), (179, 130, 75))
    regions = large_regions(dark, "large_dark_region")
    ring = house_ring(rgb)
    if ring["status"] == "candidate":
        height, width = rgb.shape[:2]
        yy, xx = np.indices((height, width), dtype=np.float32)
        dx, dy = xx - ring["center_px"][0], yy - ring["center_px"][1]
        angle = np.deg2rad(ring["angle_degrees"])
        first, second = ring["axes_px"]
        radius = np.sqrt(
            ((dx * np.cos(angle) + dy * np.sin(angle)) / (first / 2)) ** 2
            + ((-dx * np.sin(angle) + dy * np.cos(angle)) / (second / 2)) ** 2
        )
        blue = cv2.inRange(hsv, (90, 60, 45), (140, 255, 255)) > 0
        # Infer the painted band from the majority around the fitted ellipse;
        # fixed inner radii would misclassify narrower rings as an obstruction.
        bins = np.minimum((radius * 100).astype(np.int32), 130)
        counts = np.bincount(bins.ravel(), minlength=131)
        blue_counts = np.bincount(bins[blue], minlength=131)
        fractions = blue_counts / np.maximum(counts, 1)
        runs, run = [], []
        for index in range(50, 102):
            if fractions[index] >= 0.7:
                run.append(index)
            else:
                if len(run) >= 10:
                    runs.append(run)
                run = []
        if len(run) >= 10:
            runs.append(run)
        if runs:
            band = max(runs, key=lambda r: r[-1])
            inner, outer = (band[0] + 2) / 100, (band[-1] - 1) / 100
            ring = {**ring, "band_inner_radius": inner, "band_outer_radius": outer}
            annulus = (radius > inner) & (radius < outer)
            obstruction = (annulus & ~blue).astype(np.uint8) * 255
            regions.extend(large_regions(obstruction, "blue_ring_obstruction"))
        else:
            ring = {**ring, "status": "review_required", "missing_reason": "blue_band_not_supported"}
    return {"foreground_regions": regions, "ring_reference": ring, "visibility_version": VERSION}


def foreground_regions(rgb):
    return visibility_observation(rgb)["foreground_regions"]
