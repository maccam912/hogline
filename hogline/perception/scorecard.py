"""Read Wausau's current-end and blank-end plates independently of cumulative scores."""

import re

import cv2
import numpy as np

from hogline.perception.ocr import normalized_crop

VERSION = "wausau-middle-row-plates-v3"


def fixed_header_cells(image, template):
    board = normalized_crop(image, template["bounds"])
    bounds = template["layout_reference"]["header_bounds"]
    header = normalized_crop(board, [0, bounds[1], 1, bounds[3]])
    centers = template["point_centers"]
    result = {}
    for index, center in enumerate(centers):
        left = (centers[index - 1] + center) / 2 if index else center - (centers[1] - center) / 2
        right = (
            (center + centers[index + 1]) / 2
            if index + 1 < len(centers)
            else center + (center - centers[-2]) / 2
        )
        result[index + 1] = normalized_crop(header, [left, 0, right, 1])
    return result


def fixed_glyph_correlation(cell, reference):
    first = np.asarray(cell.convert("L").resize((32, 64)), dtype=np.float32)
    second = np.asarray(reference.convert("L").resize((32, 64)), dtype=np.float32)
    scores = []
    for dx in range(-3, 4):
        for dy in range(-5, 6):
            x0, x1 = max(0, dx), min(32, 32 + dx)
            y0, y1 = max(0, dy), min(64, 64 + dy)
            a, b = first[y0:y1, x0:x1], second[y0 - dy : y1 - dy, x0 - dx : x1 - dx]
            if min(float(a.std()), float(b.std())) >= 5:
                scores.append(float(np.corrcoef(a.ravel(), b.ravel())[0, 1]))
    return max(scores, default=0)


def numeral(image, reader):
    alternatives = [reader.read(image, whitelist="0123456789", psm=psm) for psm in (10, 13)]
    numbers = []
    for alternative in alternatives:
        value = re.sub(r"\s", "", alternative["text"])
        if value.isdigit() and 1 <= int(value) <= 16:
            numbers.append(int(value))
    return numbers[0] if numbers and len(set(numbers)) == 1 else None, alternatives


def white_plate(cell, *, current=False):
    """Printed white numerals are sparse; a hanging plate occupies a solid rectangle."""
    rgb = np.asarray(cell.convert("RGB"))
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    mask = cv2.inRange(hsv, (0, 0, 135), (179, 85, 255))
    count = float(np.mean(mask > 0))
    _, _, components, _ = cv2.connectedComponentsWithStats(mask)
    bounds = (
        tuple(map(int, max(components[1:], key=lambda c: c[4])[:4])) if len(components) > 1 else (0, 0, 0, 0)
    )
    x, y, w, h = bounds
    rectangle = rgb[y : y + h, x : x + w].astype(int)
    neutral = float(np.mean(np.max(rectangle, axis=2) - np.min(rectangle, axis=2) <= 25)) if w and h else 0
    candidate = (
        count >= (0.15 if current else 0.40)
        and h / cell.height >= 0.65
        and w / cell.width >= (0.25 if current else 0.35)
        and neutral >= 0.85
    )
    inset = max(1, round(min(w, h) * 0.08))
    cropped = (
        cell.crop(
            (x + inset, y + inset, max(x + inset + 1, x + w - inset), max(y + inset + 1, y + h - inset))
        )
        if candidate
        else None
    )
    return cropped, {
        "white_fraction": count,
        "white_bounds_px": list(bounds),
        "achromatic_rectangle_fraction": neutral,
        "status": "plate_candidate" if candidate else "no_plate_candidate",
    }


def read_middle_row(image, template, reader, fixed_reference=None):
    board = normalized_crop(image, template["bounds"])
    centers = template["point_centers"]
    rows = template["rows"]
    top, bottom = rows["red"][1], rows["yellow"][0]
    header = normalized_crop(board, [0, top, 1, bottom])
    step = centers[1] - centers[0]
    slots = [(0, 0, max(0.01, centers[0] - step / 2))]
    for index, center in enumerate(centers):
        left = (centers[index - 1] + center) / 2 if index else center - step / 2
        right = (
            (center + centers[index + 1]) / 2
            if index + 1 < len(centers)
            else center + (center - centers[-2]) / 2
        )
        slots.append((index + 1, left, right))
    current, blanks, unresolved, checks = None, [], [], []
    for column, left, right in slots:
        cell = normalized_crop(header, [left, 0, right, 1])
        plate, check = white_plate(cell, current=column == 0)
        check = {**check, "column": column, "normalized_board_bounds": [left, top, right, bottom]}
        if column and fixed_reference and column in fixed_reference:
            correlation = fixed_glyph_correlation(cell, fixed_reference[column])
            check["fixed_header_correlation"] = correlation
            if correlation >= 0.55:
                plate = None
                check["status"] = "fixed_printed_number; not_blank_plate"
        if plate is None:
            checks.append(check)
            continue
        number, alternatives = numeral(plate, reader)
        check = {**check, "end_number": number, "ocr_alternatives": alternatives}
        checks.append(check)
        if column == 0:
            current = {**check, "number": number, "meaning": "current_end; posting_can_lag_play"}
        elif number == column:
            blanks.append(
                {
                    **check,
                    "identity_basis": "front_plate_matches_covered_center_number",
                    "acceptance": "proposed",
                }
            )
        else:
            gray = cv2.cvtColor(np.asarray(plate.convert("RGB")), cv2.COLOR_RGB2GRAY)
            dark_fraction = float(np.mean(gray < 100))
            unresolved.append(
                {
                    **check,
                    "column_hint": column,
                    "status": "reversed_blank_plate_candidate"
                    if number is None and dark_fraction < 0.08
                    else "unreadable_or_conflicting_center_plate",
                    "dark_fraction": dark_fraction,
                    "acceptance": "proposed",
                }
            )
    return {
        "version": VERSION,
        "current_end": current,
        "blank_end_cards": blanks,
        "unassigned_blank_plates": unresolved,
        "middle_slot_visibility": checks,
        "club_convention": "center_row_plate_marks_blank; far_left_plate_is_current_end; posting_may_lag",
    }


def existing_template(observation, layout):
    board = observation.get("scoreboard", {})
    if board.get("status") != "observed":
        return None
    registered = board.get("header_registration", {}).get("template")
    if registered:
        return registered
    template = layout.get("broadcast", {}).get("scoreboards", {}).get(str(observation.get("sheet")))
    if template and board.get("effective_bounds"):
        return {**template, "bounds": board["effective_bounds"]}
    return None
