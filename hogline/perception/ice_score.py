"""Registered empty-ice background and explicit stone-body score measurements."""

import math

import cv2
import numpy as np
from scipy.ndimage import binary_fill_holes

from hogline.perception.house import house_ring
from hogline.perception.house_score import normalized_point, score_footprints
from hogline.perception.stones import RawHSVHandleDetector

VERSION = "registered-background-ice-score-v1"


class IceInspector:
    def __init__(self, reference_rgb, reference):
        if reference.get("status") != "visually_reviewed_empty_scoring_house":
            raise ValueError("An explicitly reviewed empty-house reference is required")
        self.reference_rgb = np.asarray(reference_rgb, dtype=np.uint8)
        self.reference = reference
        self.detector = RawHSVHandleDetector()

    def inspect(self, rgb):
        rgb = np.asarray(rgb, dtype=np.uint8)
        height, width = rgb.shape[:2]
        reference = cv2.resize(self.reference_rgb, (width, height), interpolation=cv2.INTER_CUBIC)
        ring = house_ring(reference)
        if ring["status"] != "candidate":
            return {
                "version": VERSION,
                "status": "unobservable",
                "reason": "reference_house_geometry_unavailable",
            }
        current_gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY).astype(np.float32)
        reference_gray = cv2.cvtColor(reference, cv2.COLOR_RGB2GRAY).astype(np.float32)
        hann = cv2.createHanningWindow((width, height), cv2.CV_32F)
        (dx, dy), response = cv2.phaseCorrelate(reference_gray, current_gray, hann)
        if (
            not all(math.isfinite(v) for v in (dx, dy, response))
            or max(abs(dx), abs(dy)) > width * 0.025
            or response < 0.15
        ):
            return {
                "version": VERSION,
                "status": "unobservable",
                "reason": "camera_or_background_alignment_unresolved",
                "phase_response": float(response),
                "shift": [float(dx), float(dy)],
            }
        matrix = np.array([[1, 0, -dx], [0, 1, -dy]], dtype=np.float32)
        aligned = cv2.warpAffine(
            rgb, matrix, (width, height), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT
        )
        yy, xx = np.indices((height, width), dtype=np.float32)
        angle = math.radians(ring["angle_degrees"])
        px, py = xx - ring["center_px"][0], yy - ring["center_px"][1]
        radius = np.sqrt(
            ((px * np.cos(angle) + py * np.sin(angle)) / (ring["axes_px"][0] / 2)) ** 2
            + ((-px * np.sin(angle) + py * np.cos(angle)) / (ring["axes_px"][1] / 2)) ** 2
        )
        # All scoring ice plus a narrow margin. Parked stones outside the house
        # can differ from an empty-house reference without occupying scoring ice.
        roi = radius <= 1.02
        normalized = aligned.astype(np.float32)
        gains, shifts = [], []
        for channel in range(3):
            current = aligned[:, :, channel][roi][::5].astype(float)
            target = reference[:, :, channel][roi][::5].astype(float)
            low, high = np.percentile(current, (15, 85))
            ref_low, ref_high = np.percentile(target, (15, 85))
            gain = float((ref_high - ref_low) / max(10, high - low))
            offset = float(ref_low - gain * low)
            for _ in range(3):
                stable = np.abs(current * gain + offset - target) < 18
                if np.mean(stable) < 0.55:
                    return {
                        "version": VERSION,
                        "status": "unobservable",
                        "reason": "too_little_matching_reference_background",
                    }
                x, y = current[stable], target[stable]
                variance = float(np.var(x))
                if variance < 100:
                    return {
                        "version": VERSION,
                        "status": "unobservable",
                        "reason": "lighting_fit_lacks_background_contrast",
                    }
                gain = float(np.mean((x - x.mean()) * (y - y.mean())) / variance)
                offset = float(y.mean() - gain * x.mean())
            if not 0.7 <= gain <= 1.45 or abs(offset) > 65:
                return {
                    "version": VERSION,
                    "status": "unobservable",
                    "reason": "lighting_outside_reference_range",
                }
            normalized[:, :, channel] = normalized[:, :, channel] * gain + offset
            gains.append(gain)
            shifts.append(offset)
        difference = np.max(np.abs(normalized - reference.astype(np.float32)), axis=2)
        changed = ((difference > 22) & roi).astype(np.uint8) * 255
        changed = cv2.morphologyEx(changed, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        changed = cv2.morphologyEx(changed, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
        changed = binary_fill_holes(changed > 0).astype(np.uint8) * 255
        count, labels, stats, _ = cv2.connectedComponentsWithStats(changed)
        minimum_body = math.pi * (width * 0.018) ** 2
        largest = max((int(s[4]) for s in stats[1:]), default=0)
        empty = largest < minimum_body * 0.35 and float(np.mean(changed[roi] > 0)) < 0.004
        result = {
            "version": VERSION,
            "status": "observed",
            "reference_evidence_id": self.reference["evidence_id"],
            "ring": ring,
            "alignment": {
                "current_to_reference": matrix.tolist(),
                "phase_response": float(response),
                "gains": gains,
                "offsets": shifts,
            },
            "changed_fraction": float(np.mean(changed[roi] > 0)),
            "largest_changed_component": largest,
            "empty_house": empty,
            "footprints": [],
            "excluded_static_handles": 0,
        }
        if empty:
            result["scoring"] = {
                "status": "proposed",
                "provisional_score": {"red": 0, "yellow": 0},
                "reasons": [],
                "method": "registered_reviewed_empty_house_match",
            }
            return result
        handles = self.detector.detect(rgb)
        fitted, blockers, represented = [], [], set()
        for index, handle in enumerate(handles):
            point = np.asarray(handle["center_px"]) - [dx, dy]
            if np.linalg.norm(normalized_point(point, ring)) > 1.16:
                continue
            x, y = np.rint(point).astype(int)
            if not 0 <= x < width or not 0 <= y < height:
                continue
            component = int(labels[y, x])
            if not component:
                patch = labels[max(0, y - 4) : min(height, y + 5), max(0, x - 4) : min(width, x + 5)]
                values, frequencies = np.unique(patch[patch > 0], return_counts=True)
                component = int(values[np.argmax(frequencies)]) if len(values) else 0
            if not component:
                result["excluded_static_handles"] += 1
                continue
            represented.add(component)
            mask = (labels == component).astype(np.uint8) * 255
            contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
            contour = max(contours, key=cv2.contourArea)
            (cx, cy), body_radius = cv2.minEnclosingCircle(contour)
            area = float(cv2.contourArea(contour))
            circular = area / max(1, math.pi * body_radius**2)
            if (
                not width * 0.018 <= body_radius <= width * 0.070
                or circular < 0.70
                or math.dist((cx, cy), point) > body_radius * 0.8
            ):
                blockers.append("unresolved_body_or_foreground_at_handle")
                continue
            radial = np.linalg.norm(contour[:, 0].astype(float) - [cx, cy], axis=1)
            error = 2 + float(np.percentile(radial, 95) - np.percentile(radial, 5))
            fitted.append(
                {
                    "status": "candidate",
                    "color": handle["color"],
                    "handle_index": index,
                    "center_px": [float(cx), float(cy)],
                    "radius_px": float(body_radius),
                    "source_center_px": [float(cx + dx), float(cy + dy)],
                    "boundary_error_px": error,
                    "component_area": area,
                    "circular_fill": circular,
                    "component_id": component,
                }
            )
        for component, stat in enumerate(stats[1:], 1):
            if stat[4] >= minimum_body * 0.6 and component not in represented:
                blockers.append("unexplained_changed_object_inside_scoring_region")
        for component in represented:
            if stats[component][4] > math.pi * (width * 0.070) ** 2 * 1.3:
                blockers.append("foreground_or_touching_bodies_unresolved")
        scoring = score_footprints(fitted, ring, uncertainty=0.015)
        if blockers or not fitted:
            scoring = {
                **scoring,
                "status": "unresolved",
                "provisional_score": None,
                "reasons": sorted(
                    set(
                        scoring["reasons"] + blockers + ["no_supported_bodies"]
                        if not fitted
                        else scoring["reasons"] + blockers
                    )
                ),
            }
        result["footprints"], result["scoring"] = fitted, scoring
        result["limitations"] = [
            "background_calibration_is_development_data",
            "stable_house_is_not_necessarily_final_end_state",
        ]
        return result
