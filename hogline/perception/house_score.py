"""Experimental image-space granite boundaries and uncertainty-aware score proposals."""

import math

import cv2
import numpy as np

VERSION = "image-space-granite-score-v3"


def normalized_point(point, ring):
    angle = math.radians(ring["angle_degrees"])
    x, y = np.asarray(point, dtype=float) - ring["center_px"]
    return np.array(
        [
            (x * math.cos(angle) + y * math.sin(angle)) / (ring["axes_px"][0] / 2),
            (-x * math.sin(angle) + y * math.cos(angle)) / (ring["axes_px"][1] / 2),
        ]
    )


def score_footprints(footprints, ring, *, uncertainty=0.025):
    """Distance intervals use estimated granite edges, never handle distance alone.

    Coordinates are affine-normalized by the fitted outer house ellipse. This is
    deliberately not a surveyed or projectively calibrated scoring measurement.
    """
    unresolved = {"status": "unresolved", "provisional_score": None, "measurements": []}
    if ring.get("status") != "candidate":
        return {**unresolved, "reasons": ["house_geometry_unavailable"]}
    if (
        not math.isfinite(uncertainty)
        or uncertainty < 0
        or any(not math.isfinite(float(v)) or v <= 0 for v in ring.get("axes_px", []))
        or len(ring.get("axes_px", [])) != 2
        or len(ring.get("center_px", [])) != 2
        or not all(math.isfinite(float(v)) for v in ring["center_px"])
        or not math.isfinite(ring.get("angle_degrees", float("nan")))
    ):
        raise ValueError("House scoring requires finite, positive geometry and uncertainty")
    if not footprints:
        return {**unresolved, "reasons": ["zero_handles_does_not_establish_blank_house"]}
    measurements, reasons = [], []
    for footprint in footprints:
        if footprint.get("status") != "candidate" or footprint.get("color") not in ("red", "yellow"):
            reasons.append("unresolved_granite_boundary_or_color")
            continue
        center = np.asarray(footprint["center_px"], dtype=float)
        radius = footprint["radius_px"]
        if (
            center.shape != (2,)
            or not np.all(np.isfinite(center))
            or not math.isfinite(radius)
            or radius <= 0
        ):
            raise ValueError("Granite footprints need finite centers and positive radii")
        points = center + radius * np.column_stack(
            (np.cos(np.arange(128) * math.tau / 128), np.sin(np.arange(128) * math.tau / 128))
        )
        distances = np.linalg.norm([normalized_point(p, ring) for p in points], axis=1)
        # A stone covering the tee has distance zero to the nearest part of its footprint.
        nearest = 0.0 if np.linalg.norm(center - ring["center_px"]) <= radius else float(min(distances))
        error = uncertainty + footprint.get("boundary_error_px", 2) / (min(ring["axes_px"]) / 2)
        low, high = max(0.0, nearest - error), nearest + error
        eligibility = "inside" if high < 1 else "outside" if low > 1 else "boundary_uncertain"
        measurements.append(
            {
                "color": footprint["color"],
                "handle_index": footprint.get("handle_index"),
                "nearest_edge_interval_house_radii": [low, high],
                "eligibility": eligibility,
            }
        )
        if eligibility == "boundary_uncertain":
            reasons.append("house_biter_requires_measurement")
    if reasons:
        return {**unresolved, "measurements": measurements, "reasons": sorted(set(reasons))}
    eligible = [m for m in measurements if m["eligibility"] == "inside"]
    if not eligible:
        return {
            "status": "proposed",
            "provisional_score": {"red": 0, "yellow": 0},
            "measurements": measurements,
            "reasons": [],
        }
    limits = {
        color: [m["nearest_edge_interval_house_radii"] for m in eligible if m["color"] == color]
        for color in ("red", "yellow")
    }
    closest = {
        c: (min(x[0] for x in values), min(x[1] for x in values)) if values else (math.inf, math.inf)
        for c, values in limits.items()
    }
    winner = next((c for c in limits if closest[c][1] < closest["yellow" if c == "red" else "red"][0]), None)
    if winner is None:
        return {**unresolved, "measurements": measurements, "reasons": ["nearest_colors_require_measurement"]}
    opponent = "yellow" if winner == "red" else "red"
    certain = sum(hi < closest[opponent][0] for lo, hi in limits[winner])
    possible = sum(lo < closest[opponent][1] for lo, hi in limits[winner])
    if certain != possible:
        return {**unresolved, "measurements": measurements, "reasons": ["point_count_requires_measurement"]}
    if certain > 8:
        return {**unresolved, "measurements": measurements, "reasons": ["stone_count_exceeds_standard_end"]}
    return {
        "status": "proposed",
        "provisional_score": {winner: certain, opponent: 0},
        "measurements": measurements,
        "reasons": [],
    }


def granite_footprint(rgb, handle, index):
    """Fit local circular edge support around a colored handle; retain failure diagnostics."""
    height, width = rgb.shape[:2]
    seed = np.asarray(handle["center_px"], dtype=float)
    base = {
        "handle_index": index,
        "color": handle["color"],
        "handle_center_px": seed.tolist(),
        "status": "unresolved",
        "object_kind": "experimental_granite_footprint",
    }
    if handle.get("boundary_clipped"):
        return {**base, "reason": "clipped_handle"}
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    edges = cv2.Canny(gray, 45, 100)
    near_edges = cv2.dilate(edges, np.ones((3, 3), np.uint8))
    angles = np.arange(48) * math.tau / 48
    directions = np.column_stack((np.cos(angles), np.sin(angles)))
    best = None
    # This size/offset prior is explicit and provisional, not an inferred physical diameter.
    for dx in (-4, 0, 4):
        for dy in (-4, 0, 4):
            center = seed + [dx, dy]
            box = handle.get("bbox_px")
            # A granite edge must extend beyond the colored component. Otherwise the
            # fit can lock onto the plastic handle and call that the stone boundary.
            outside_handle = 0
            if box:
                x, y, w, h = box
                outside_handle = math.ceil(
                    max(
                        abs(center[0] - x), abs(x + w - center[0]), abs(center[1] - y), abs(y + h - center[1])
                    )
                    + 2
                )
            minimum_radius = max(5, round(width * 0.022), outside_handle)
            for radius in range(minimum_radius, max(6, round(width * 0.060)), 2):
                points = np.rint(center + radius * directions).astype(int)
                if (
                    points[:, 0].min() < 1
                    or points[:, 1].min() < 1
                    or points[:, 0].max() >= width - 1
                    or points[:, 1].max() >= height - 1
                ):
                    continue
                support = near_edges[points[:, 1], points[:, 0]] > 0
                quadrant_support = [float(np.mean(p)) for p in np.split(support, 4)]
                fraction = float(np.mean(support))
                # Every side needs edge support; logos/rings are retained as a known failure mode.
                quality = fraction + 0.1 * min(quadrant_support)
                if best is None or quality > best[0]:
                    best = (quality, center.tolist(), radius, fraction, quadrant_support)
    if best is None or best[3] < 0.72 or min(best[4]) < 0.5:
        return {
            **base,
            "reason": "insufficient_granite_perimeter_support",
            "edge_support_fraction": best[3] if best else None,
        }
    return {
        **base,
        "status": "candidate",
        "center_px": best[1],
        "radius_px": best[2],
        "boundary_error_px": 5,
        "edge_support_fraction": best[3],
        "quadrant_edge_support": best[4],
        "reason": None,
    }


def inspect_house_score(rgb, view):
    from hogline.perception.visibility import visibility_observation

    handles = view["detections"]
    ring = view["house_ring"]
    if len(handles) > 32:
        return {
            "footprints": [],
            "ring_reference": ring,
            "visibility": {},
            "scoring": {
                "status": "unresolved",
                "provisional_score": None,
                "measurements": [],
                "reasons": ["too_many_handle_candidates"],
            },
            "acceptance": "proposed",
            "end_phase": "unknown",
            "official_end_number": None,
            "limitations": ["stone_detection_completeness_unverified"],
        }
    footprints = [granite_footprint(rgb, d, i) for i, d in enumerate(handles)]
    visibility = visibility_observation(rgb)
    blockers = []
    if visibility.get("foreground_regions"):
        blockers.append("foreground_obstruction")
    if view.get("rejected_candidates"):
        blockers.append("unresolved_color_candidates")
    scoring = score_footprints(
        footprints,
        visibility["ring_reference"],
        uncertainty=max(0.025, ring.get("median_normalized_residual", 0)),
    )
    if blockers:
        scoring = {
            **scoring,
            "status": "unresolved",
            "provisional_score": None,
            "reasons": sorted(set(scoring["reasons"] + blockers)),
        }
    return {
        "footprints": footprints,
        "ring_reference": ring,
        "visibility": visibility,
        "scoring": scoring,
        "acceptance": "proposed",
        "end_phase": "unknown",
        "official_end_number": None,
        "limitations": [
            "stone_detection_completeness_unverified",
            "image_space_geometry_not_surveyed",
            "footprint_edges_may_be_clothing_or_painted_ice",
            "pre_cleanup_state_unverified",
        ],
    }
