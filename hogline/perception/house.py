"""Image-space house checks; an ellipse is not a surveyed sheet calibration."""

import math

import cv2
import numpy as np


def house_ring(rgb):
    height, width = rgb.shape[:2]
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    blue = cv2.inRange(hsv, (90, 60, 45), (140, 255, 255))
    contours, _ = cv2.findContours(blue, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    candidates = [c for c in contours if len(c) >= 20 and cv2.contourArea(c) > width * height * 0.12]
    if not candidates:
        return {"status": "unavailable", "missing_reason": "house_ring_not_found_in_seed_crop"}
    contour = max(candidates, key=cv2.contourArea)
    (cx, cy), (major, minor), angle = cv2.fitEllipse(contour)
    if min(major, minor) <= 0:
        return {"status": "unavailable", "missing_reason": "degenerate_house_ellipse"}
    theta = math.radians(angle)
    points = contour[:, 0].astype(np.float64) - [cx, cy]
    rotated_x = points[:, 0] * math.cos(theta) + points[:, 1] * math.sin(theta)
    rotated_y = -points[:, 0] * math.sin(theta) + points[:, 1] * math.cos(theta)
    radii = np.sqrt((rotated_x / (major / 2)) ** 2 + (rotated_y / (minor / 2)) ** 2)
    residual = float(np.median(np.abs(radii - 1)))
    usable = (
        0.15 * width <= cx <= 0.85 * width
        and 0.15 * height <= cy <= 0.85 * height
        and 0.45 * width <= major <= 1.3 * width
        and 0.45 * height <= minor <= 1.3 * height
        and residual < 0.12
    )
    return {
        "status": "candidate" if usable else "review_required",
        "center_px": [cx, cy],
        "axes_px": [major, minor],
        "angle_degrees": angle,
        "median_normalized_residual": residual,
        "acceptance": "proposed",
        "metric_calibration": None,
        "limitations": "blue-ring image fit; physical dimensions and A/B end mapping unverified",
    }
