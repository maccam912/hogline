"""Image-space layout checks; alignment is not score or metric calibration."""

import hashlib
import math

import cv2
import numpy as np
from PIL import Image
from sqlalchemy import select

from hogline.db import Evidence, Source
from hogline.perception.ocr import normalized_crop


def reference_image(session, root, template):
    reference = template.get("layout_reference")
    if not reference:
        return None, {"status": "unavailable", "missing_reason": "layout_reference_not_configured"}
    source = session.scalar(select(Source).where(Source.source_key == reference["source_key"]))
    if source:
        target = reference["source_seconds"]
        frames = list(
            session.scalars(
                select(Evidence).where(
                    Evidence.source_id == source.id,
                    Evidence.document["source_seconds"].as_float().between(target - 0.1, target + 0.1),
                )
            )
        )
        frames.sort(key=lambda f: f.document["image"]["height"], reverse=True)
        for frame in frames:
            doc = frame.document
            if doc.get("acquisition_tier") == "coarse":
                continue
            asset = doc["image"]
            path = root / asset["path"]
            if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == asset["sha256"]:
                return Image.open(path).convert("RGB"), {
                    "evidence_id": frame.id,
                    "image_sha256": asset["sha256"],
                    "source_key": reference["source_key"],
                    "source_seconds": doc["source_seconds"],
                    "youtube_url": doc.get("youtube_url"),
                }
    return None, {"status": "unavailable", "missing_reason": "retained_layout_reference_missing_or_corrupt"}


def board_alignment(image, reference, template):
    header_bounds = template["layout_reference"]["header_bounds"]
    seed_bounds = template["bounds"]

    def patch(full, bounds):
        board = normalized_crop(full, bounds)
        header = normalized_crop(board, header_bounds)
        data = np.asarray(header.convert("L").resize((288, 40)), dtype=np.float32)
        return data, header.size

    original, _ = patch(reference, seed_bounds)

    def measure(bounds):
        current, size = patch(image, bounds)
        if min(float(original.std()), float(current.std())) < 8:
            return None, size
        window = cv2.createHanningWindow((288, 40), cv2.CV_32F)
        shift, response = cv2.phaseCorrelate(original.copy(), current.copy(), window)
        correlation = float(np.corrcoef(original.flatten(), current.flatten())[0, 1])
        if not all(math.isfinite(v) for v in [*shift, response, correlation]):
            return None, size
        return {
            "shift_canonical_px": [float(v) for v in shift],
            "phase_response": float(response),
            "header_correlation": correlation,
        }, size

    def compatible(result):
        return (
            abs(result["shift_canonical_px"][0]) <= 1.5
            and abs(result["shift_canonical_px"][1]) <= 1.5
            and result["phase_response"] >= 0.2
            and result["header_correlation"] >= 0.75
        )

    first, size = measure(seed_bounds)
    if first is None:
        return {"status": "unavailable", "missing_reason": "fixed_header_texture_not_visible"}
    result = {
        **first,
        "status": "compatible_seed" if compatible(first) else "drift_or_occlusion",
        "effective_bounds": seed_bounds,
        "canonical_header_size": [288, 40],
        "method": "fixed_board_header_alignment_v2",
        "calibrated_probability": None,
        "limitation": "experimental fixed-header gate; not a validated layout detector",
    }
    sx, sy = first["shift_canonical_px"]
    if (
        result["status"] != "compatible_seed"
        and abs(sx) <= 12
        and abs(sy) <= 8
        and first["phase_response"] >= 0.65
    ):
        base_x, base_y = round(sx * size[0] / 288), round(sy * size[1] / 40)
        trials = []
        for dx_px in range(base_x - 1, base_x + 2):
            for dy_px in range(base_y - 1, base_y + 2):
                dx, dy = dx_px / image.width, dy_px / image.height
                bounds = [seed_bounds[0] + dx, seed_bounds[1] + dy, seed_bounds[2] + dx, seed_bounds[3] + dy]
                if all(0 <= v <= 1 for v in bounds):
                    corrected, _ = measure(bounds)
                    if corrected:
                        trials.append((corrected, bounds, [dx_px, dy_px]))
        if trials:
            corrected, bounds, translation = max(trials, key=lambda trial: trial[0]["header_correlation"])
            result = {
                **result,
                "after_translation": corrected,
                "proposed_translation_source_px": translation,
                "refinement": "nearest_pixel_3x3_verified",
            }
            if compatible(corrected) and corrected["header_correlation"] >= 0.85:
                result = {
                    **result,
                    "status": "translated_seed",
                    "effective_bounds": bounds,
                    "translation_source_px": translation,
                }
    return result
