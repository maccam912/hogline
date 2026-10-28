"""Measured dark regions flag possible obstruction, not detected people or clear ice."""

import cv2
import numpy as np


def foreground_regions(rgb):
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    # Low value and modest saturation avoid treating blue/green house rings as
    # clothing. Logos and clustered granite can still trigger this conservative
    # gate; no region is classified as a person or assigned an identity.
    mask = cv2.inRange(hsv, (0, 0, 0), (179, 130, 75))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    height, width = rgb.shape[:2]
    count, _, stats, _ = cv2.connectedComponentsWithStats(mask)
    regions = []
    for x, y, w, h, area in stats[1:count]:
        if area >= width * height * 0.01 and max(w / width, h / height) >= 0.15:
            regions.append({"bbox_px": [int(x), int(y), int(w), int(h)], "area_px2": int(area)})
    return regions
