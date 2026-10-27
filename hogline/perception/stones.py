import math
from typing import Protocol

import cv2
import numpy as np

VERSION = "opencv-hsv-handles-v3"


class StoneDetector(Protocol):
    version: str

    def detect(self, rgb: np.ndarray) -> list[dict]: ...


class HSVHandleDetector:
    """Cheap benchmark to compare against a future trained instance detector.

    These are colored HANDLE candidates in crop pixels, not stone footprints,
    persistent physical IDs, distances in meters, or calibrated probabilities.
    """

    version = VERSION

    def detect(self, rgb):
        hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
        height, width = rgb.shape[:2]
        minimum = max(6, width * height * 0.00008)
        maximum = width * height * 0.008
        colors = {
            "red": cv2.inRange(hsv, (0, 75, 60), (12, 255, 255))
            | cv2.inRange(hsv, (165, 75, 60), (179, 255, 255)),
            "yellow": cv2.inRange(hsv, (17, 65, 100), (40, 255, 255)),
        }
        detections = []
        for color, mask in colors.items():
            # Opening rejects isolated noise without joining adjacent handles.
            mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
            # A red broadcast border can enclose otherwise disconnected red handles.
            # RETR_EXTERNAL would silently discard those interior components.
            contours, _ = cv2.findContours(mask, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
            for contour in contours:
                area = cv2.contourArea(contour)
                if not minimum <= area <= maximum:
                    continue
                x, y, w, h = cv2.boundingRect(contour)
                perimeter = cv2.arcLength(contour, True)
                circularity = 4 * math.pi * area / max(1, perimeter**2)
                if not (0.45 <= w / h <= 2 and circularity >= 0.3 and max(w, h) < width * 0.13):
                    continue
                moments = cv2.moments(contour)
                center = [moments["m10"] / moments["m00"], moments["m01"] / moments["m00"]]
                detections.append(
                    {
                        "color": color,
                        "center_px": center,
                        "bbox_px": [x, y, w, h],
                        "area_px2": area,
                        "shape_score": min(1.0, circularity),
                        "score_kind": "uncalibrated_circularity",
                        "boundary_clipped": x <= 1 or y <= 1 or x + w >= width - 1 or y + h >= height - 1,
                        "acceptance": "proposed",
                        "object_kind": "colored_handle_candidate",
                    }
                )
        return sorted(detections, key=lambda d: (d["color"], d["center_px"][1], d["center_px"][0]))
