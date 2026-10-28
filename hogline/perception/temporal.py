"""Conservative image-space persistence, without physical identity or scoring claims."""

import math

import numpy as np
from scipy.optimize import linear_sum_assignment

VERSION = "handle-configuration-persistence-v2"
MIN_SAMPLES = 4
MIN_SECONDS = 1.5
MAX_MOTION = 2.5  # Mean absolute grayscale difference on 96x96 house crops, 0..255.


def match_configuration(before, after, width):
    if not before or len(before) != len(after):
        return False
    if any(d["boundary_clipped"] for d in before + after):
        return False
    tolerance = max(2, width * 0.006)
    costs = np.array(
        [
            [math.dist(a["center_px"], b["center_px"]) if a["color"] == b["color"] else 1e9 for b in after]
            for a in before
        ]
    )
    rows, columns = linear_sum_assignment(costs)
    return bool(np.all(costs[rows, columns] <= tolerance))


class ConfigurationTracker:
    """Require the entire visible candidate set to persist, not merely its count.

    Runs split on motion, sampling gaps, clipped objects, ambiguous absence, or
    position/color changes. Comparing to both first and previous configurations
    prevents slow drift from becoming a settled-state claim.
    """

    def __init__(self):
        self.runs = {}
        self.completed = []

    def candidate(self, view):
        run = self.runs.get(view, [])
        if len(run) < MIN_SAMPLES or run[-1]["source_seconds"] - run[0]["source_seconds"] < MIN_SECONDS:
            return None
        return {
            "kind": "persistent_handle_configuration",
            "view": view,
            "start_seconds": run[0]["source_seconds"],
            "end_seconds": run[-1]["source_seconds"],
            "sample_indices": [r["sample_index"] for r in run],
            "candidate_count": len(run[0]["detections"]),
            "acceptance": "proposed",
            "score": None,
            "missing_reason": "uncalibrated_handles_visibility_and_end_phase_unresolved",
        }

    def feed(self, row):
        view = row["view"]
        run = self.runs.setdefault(view, [])
        usable = (
            bool(row["detections"])
            and not row["foreground_regions"]
            and not any(d["boundary_clipped"] for d in row["detections"])
        )
        if run:
            previous = run[-1]
            gap = row["source_seconds"] - previous["source_seconds"]
            compatible = (
                usable
                and 0 < gap <= 1.5 * row["requested_interval_seconds"] + 0.05
                and row["motion_mean_abs"] is not None
                and row["motion_mean_abs"] <= MAX_MOTION
                and row["crop_width"] == previous["crop_width"]
                and row["crop_height"] == previous["crop_height"]
                and match_configuration(previous["detections"], row["detections"], row["crop_width"])
                and match_configuration(run[0]["detections"], row["detections"], row["crop_width"])
            )
            if not compatible:
                if candidate := self.candidate(view):
                    self.completed.append(candidate)
                run = self.runs[view] = []
        if usable:
            run.append(row)
        return self.candidate(view)

    def results(self):
        return sorted(
            self.completed + [c for view in self.runs if (c := self.candidate(view))],
            key=lambda c: (c["start_seconds"], c["view"]),
        )


def persistent_configurations(rows):
    tracker = ConfigurationTracker()
    for row in rows:
        tracker.feed(row)
    return tracker.results()
