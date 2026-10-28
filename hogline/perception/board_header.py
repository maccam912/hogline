"""Propose cumulative columns only when the full fixed-number header matches."""

import cv2
import numpy as np

from hogline.perception.board_locator import locate_board
from hogline.perception.ocr import normalized_crop

VERSION = "traditional-board-header-registration-v1"
CANONICAL_WIDTH = 330
CANONICAL_HEIGHT = 36


def register_header(image, candidate, reference, reference_template, panel_bounds):
    reference_location = locate_board(reference, panel_bounds)
    if reference_location["status"] != "proposed":
        return {
            "status": "unavailable",
            "missing_reason": "reference_board_geometry_unavailable",
            "method": VERSION,
        }
    ref_candidate = reference_location["selected"]

    def patch(full, geometry):
        board = normalized_crop(full, geometry["normalized_bounds"])
        header = normalized_crop(board, [0, geometry["header_bounds"][1], 1, geometry["header_bounds"][3]])
        return cv2.resize(
            np.asarray(header.convert("L")),
            (CANONICAL_WIDTH, CANONICAL_HEIGHT),
            interpolation=cv2.INTER_CUBIC,
        )

    # Exclude the storage numeral at the left; require the entire 1..15 sequence.
    ref_patch = patch(reference, ref_candidate)
    target = patch(image, candidate)
    ref_left, ref_right = ref_candidate["normalized_bounds"][0::2]
    template_left, template_right = reference_template["bounds"][0::2]
    centers = [
        (template_left + c * (template_right - template_left) - ref_left) / (ref_right - ref_left)
        for c in reference_template["point_centers"]
    ]
    if len(centers) != 15 or any(not 0 < c < 1 for c in centers):
        return {
            "status": "unavailable",
            "missing_reason": "reference_point_columns_invalid",
            "method": VERSION,
        }
    # Keep the whole first and final printed glyphs while excluding left storage.
    x0 = round((centers[0] - 0.01) * CANONICAL_WIDTH)
    x1 = min(CANONICAL_WIDTH, round((centers[-1] + 0.035) * CANONICAL_WIDTH))
    template = ref_patch[3:-3, x0:x1]
    if template.size == 0 or template.std() < 8 or target.std() < 8:
        return {
            "status": "unavailable",
            "missing_reason": "fixed_header_texture_not_visible",
            "method": VERSION,
        }
    trials = []
    for scale in np.linspace(0.94, 1.06, 25):
        resized = cv2.resize(template, (round(template.shape[1] * scale), template.shape[0]))
        scores = cv2.matchTemplate(target, resized, cv2.TM_CCOEFF_NORMED)
        _, score, _, position = cv2.minMaxLoc(scores)
        trials.append((score, position, resized))
    score, (x, y), resized = max(trials, key=lambda trial: trial[0])
    matched = target[y : y + resized.shape[0], x : x + resized.shape[1]]
    regional = []
    for a, b in zip(
        np.linspace(0, resized.shape[1], 6, dtype=int)[:-1],
        np.linspace(0, resized.shape[1], 6, dtype=int)[1:],
        strict=True,
    ):
        left, right = resized[:, a:b].astype(float), matched[:, a:b].astype(float)
        regional.append(
            float(np.corrcoef(left.ravel(), right.ravel())[0, 1])
            if min(left.std(), right.std()) >= 8
            else 0.0
        )
    scale = resized.shape[1] / template.shape[1]
    points = [(x + (c * CANONICAL_WIDTH - x0) * scale) / CANONICAL_WIDTH for c in centers]
    compatible = score >= 0.82 and min(regional) >= 0.72 and all(0 < c < 1 for c in points)
    return {
        "status": "proposed" if compatible else "review_required",
        "method": VERSION,
        "header_correlation": float(score),
        "regional_correlations": regional,
        "canonical_size": [CANONICAL_WIDTH, CANONICAL_HEIGHT],
        "matched_template_origin": [x, y],
        "horizontal_scale": scale,
        "point_centers": points if compatible else None,
        "missing_reason": None if compatible else "fixed_number_header_disagrees_or_is_obstructed",
        "calibrated_probability": None,
        "acceptance": "proposed",
        "limitation": (
            "reference-based image registration; storage markers, blank ends, "
            "and card meanings remain unverified"
        ),
        "template": {
            "bounds": candidate["normalized_bounds"],
            "rows": candidate["row_bands"],
            "point_centers": points,
            "status": "experimental_registered_header",
        }
        if compatible
        else None,
    }
