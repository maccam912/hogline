"""Small colored-handle proposals in perspective ice, with explicit granite-context cues."""

import math

import cv2
import numpy as np

from hogline.perception.tracking import track_handles

VERSION = "perspective-colored-handles-v1"
MAX_CANDIDATES = 128
TRACK_VERSION = "perspective-handle-tracklets-v1"


def perspective_handles(rgb, people=()):
    height, width = rgb.shape[:2]
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    masks = {
        "red": cv2.inRange(hsv, (0, 75, 60), (12, 255, 255))
        | cv2.inRange(hsv, (165, 75, 60), (179, 255, 255)),
        "yellow": cv2.inRange(hsv, (17, 65, 100), (40, 255, 255)),
    }
    output = []
    for color, mask in masks.items():
        # Exclude the logo/wall and clock strips of this seed composite layout.
        mask[: round(height * 0.35)] = 0
        mask[round(height * 0.98) :] = 0
        length = max(15, round(width * 0.3))
        bands = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((1, length), np.uint8))
        bands |= cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((length, 1), np.uint8))
        mask = cv2.bitwise_and(mask, cv2.bitwise_not(bands))
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for contour in contours:
            area = cv2.contourArea(contour)
            x, y, w, h = cv2.boundingRect(contour)
            perimeter = cv2.arcLength(contour, True)
            circularity = 4 * math.pi * area / max(1, perimeter**2)
            if not (
                4 <= area <= width * height * 0.004
                and 0.25 <= w / h <= 4
                and circularity >= 0.2
                and max(w, h) <= width * 0.12
            ):
                continue
            moments = cv2.moments(contour)
            center = [moments["m10"] / moments["m00"], moments["m01"] / moments["m00"]]
            radius = max(3, round(max(w, h) * 1.3))
            x1, x2 = max(0, round(center[0]) - radius), min(width, round(center[0]) + radius + 1)
            y1, y2 = max(0, round(center[1]) - radius), min(height, round(center[1]) + radius + 1)
            yy, xx = np.mgrid[y1:y2, x1:x2]
            distance = np.hypot(xx - center[0], yy - center[1])
            annulus = (distance >= max(w, h) * 0.6) & (distance <= radius)
            neighborhood = hsv[y1:y2, x1:x2]
            gray = (
                (neighborhood[:, :, 1] < 70) & (neighborhood[:, :, 2] >= 50) & (neighborhood[:, :, 2] <= 220)
            )
            support = float(gray[annulus].mean()) if annulus.any() else 0
            person_overlap = any(
                p["bbox_px"][0] <= center[0] <= p["bbox_px"][0] + p["bbox_px"][2]
                and p["bbox_px"][1] <= center[1] <= p["bbox_px"][1] + p["bbox_px"][3] * 0.75
                for p in people
            )
            near_wrist = any(
                point["index"] in (15, 16)
                and point["inside_crop"]
                and min(point["raw_visibility"], point["raw_presence"]) >= 0.7
                and math.dist(point["center_px"], center) <= max(5, max(w, h) * 1.5)
                for person in people
                for point in (person.get("body_pose") or {}).get("landmarks", [])
            )
            output.append(
                {
                    "color": color,
                    "center_px": center,
                    "bbox_px": [x, y, w, h],
                    "area_px2": float(area),
                    "shape_score": min(1.0, circularity),
                    "granite_context_fraction": support,
                    "person_overlap": person_overlap,
                    "near_wrist": near_wrist,
                    "boundary_clipped": min(x, y, width - x - w, height - y - h) <= max(w, h) / 2,
                }
            )
    if len(output) > MAX_CANDIDATES:
        return {
            "status": "unavailable",
            "missing_reason": "too_many_perspective_color_candidates",
            "detections": [],
        }
    return {
        "status": "observed",
        "missing_reason": None,
        "detections": sorted(output, key=lambda d: (d["color"], d["center_px"][1], d["center_px"][0])),
    }


def perspective_handle_tracks(rows, input_sha256):
    converted = []
    for row in rows:
        observation = row.get("perspective_handles") or {}
        converted.append(
            {
                **row,
                "detections": [
                    {**d, "raw_detection_index": i}
                    for i, d in enumerate(observation.get("detections", []))
                    if d["granite_context_fraction"] >= 0.18
                    and (not d.get("person_overlap") or d.get("near_wrist"))
                ],
                "foreground_regions": []
                if observation.get("status") == "observed"
                else [{"kind": "unreadable"}],
            }
        )
    result = track_handles(
        converted,
        input_sha256,
        views=("perspective_left", "perspective_right"),
        version=TRACK_VERSION,
        require_ring=False,
    )
    result["notice"] = (
        "Perspective colored-handle fragments with gray-neighborhood support. "
        "They can include brooms/clothing and do not establish a delivery, granite footprint or metric speed."
    )
    return result
