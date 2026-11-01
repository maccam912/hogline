"""Conservative lying-score alternatives from complete stone-boundary measurements."""

from hogline.reconstruction.score_uncertainty import score_bounds

MEASUREMENT_REASONS = {
    "house_biter_requires_measurement",
    "nearest_colors_require_measurement",
    "point_count_requires_measurement",
}


def predict_score(scoring):
    """Missing bodies/occlusion cannot be rescued by counting colored handles.

    These bounds describe the measured house, not whether the end has finished.
    They deliberately express alternatives instead of a calibrated probability.
    """
    if set(scoring.get("reasons", [])) - MEASUREMENT_REASONS:
        return {"score_options": [], "points_range": None, "confidence": "unknown"}
    points = scoring.get("provisional_score")
    if points is not None:
        options = [points]
    else:
        rows = scoring.get("measurements", [])
        if not rows:
            return {"score_options": [], "points_range": None, "confidence": "unknown"}
        options = []
        for color, opponent in (("red", "yellow"), ("yellow", "red")):
            own = [m["nearest_edge_interval_house_radii"] for m in rows if m["color"] == color]
            other = [m["nearest_edge_interval_house_radii"] for m in rows if m["color"] == opponent]
            lower_cutoff = min([1] + [lo for lo, hi in other])
            upper_cutoff = min([1] + [hi for lo, hi in other])
            certain = sum(hi < lower_cutoff for lo, hi in own)
            possible = sum(lo < upper_cutoff for lo, hi in own)
            if possible > 8:
                return {"score_options": [], "points_range": None, "confidence": "unknown"}
            options.extend({color: n, opponent: 0} for n in range(max(1, certain), possible + 1))
        if all(m["nearest_edge_interval_house_radii"][1] >= 1 for m in rows):
            options.append({"red": 0, "yellow": 0})
    return {
        "score_options": options,
        "points_range": score_bounds(options),
        "confidence": "uncertain" if len(options) > 1 else "ice_estimate" if options else "unknown",
    }
