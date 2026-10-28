"""Traditional club boards: end-card labels and cumulative columns are distinct."""

import re
from uuid import NAMESPACE_URL, uuid5

import cv2
import numpy as np

from hogline.adapters.sources import digest
from hogline.perception.ocr import normalized_crop

CARD_VISIBILITY_VERSION = "row-color-and-card-height-v1"


def card_visibility(cell, color):
    hsv = cv2.cvtColor(np.asarray(cell.convert("RGB")), cv2.COLOR_RGB2HSV)
    white = cv2.inRange(hsv, (0, 0, 140), (179, 80, 255))
    background = (
        cv2.inRange(hsv, (17, 90, 70), (42, 255, 255))
        if color == "yellow"
        else cv2.inRange(hsv, (0, 85, 50), (13, 255, 255)) | cv2.inRange(hsv, (165, 85, 50), (179, 255, 255))
    )
    occupancy = float(np.mean(white > 0))
    top_fraction = float(np.mean(white[: max(1, round(cell.height * 0.3))] > 0))
    background_fraction = float(np.mean(background > 0))
    bounds = cv2.boundingRect(cv2.findNonZero(white)) if occupancy else (0, 0, 0, 0)
    height_fraction = bounds[3] / cell.height
    reason = None
    if occupancy >= 0.2:
        if height_fraction < 0.65 or top_fraction < 0.12:
            reason = "white_patch_not_full_height_card"
        status = "unreadable" if reason else "card_candidate"
    else:
        reason = "row_background_obstructed_or_uncertain" if background_fraction < 0.65 else None
        status = "unreadable" if reason else "no_card_candidate"
    return white, {
        "method": CARD_VISIBILITY_VERSION,
        "status": status,
        "reason": reason,
        "white_card_fraction": occupancy,
        "top_white_fraction": top_fraction,
        "white_height_fraction": height_fraction,
        "row_background_fraction": background_fraction,
        "calibrated_probability": None,
    }


def read_board(image, template, reader):
    board = normalized_crop(image, template["bounds"])
    centers = template["point_centers"]
    cards, unreadable, visibility = [], [], []
    for color, row in template["rows"].items():
        band = normalized_crop(board, [0, row[0], 1, row[1]])
        for index, center in enumerate(centers):
            left = (centers[index - 1] + center) / 2 if index else center - (centers[1] - center) / 2
            right = (
                (center + centers[index + 1]) / 2
                if index + 1 < len(centers)
                else center + (center - centers[-2]) / 2
            )
            cell = normalized_crop(band, [left, 0, right, 1])
            white, check = card_visibility(cell, color)
            visibility.append({"color": color, "cumulative_points": index + 1, **check})
            if check["status"] == "unreadable":
                unreadable.append({"color": color, "cumulative_points": index + 1, "reason": check["reason"]})
                continue
            if check["status"] == "no_card_candidate":
                continue
            # Strip colored board background and dark card borders before recognizing
            # an isolated end numeral; neither belongs to the printed glyph.
            x, y, w, h = cv2.boundingRect(cv2.findNonZero(white))
            card_image = cell.crop((x + 1, y + 1, max(x + 2, x + w - 1), max(y + 2, y + h - 1)))
            alternatives = [reader.read(card_image, whitelist="0123456789", psm=psm) for psm in (10, 13)]
            numbers = []
            for read in alternatives:
                text = re.sub(r"\s", "", read["text"])
                if text.isdigit() and 1 <= int(text) <= 16:
                    numbers.append(int(text))
            number = numbers[0] if numbers and len(set(numbers)) == 1 else None
            ocr = alternatives[0]
            card = {
                "color": color,
                "cumulative_points": index + 1,
                "end_number": number,
                "raw_text": ocr["text"],
                "ocr_alternatives": alternatives,
                "engine_confidence": ocr["engine_confidence"],
                "white_card_fraction": check["white_card_fraction"],
                "acceptance": "proposed",
                "normalized_board_bounds": [left, row[0], right, row[1]],
            }
            cards.append(card)
            if number is None:
                unreadable.append({"color": color, "cumulative_points": index + 1})
    numbers = [c["end_number"] for c in cards if c["end_number"] is not None]
    return {
        "cards": cards,
        "unreadable_slots": unreadable,
        "slot_visibility": visibility,
        "card_visibility_version": CARD_VISIBILITY_VERSION,
        "conflicting_end_numbers": sorted({n for n in numbers if numbers.count(n) > 1}),
        "acceptance": "proposed",
        "template_status": template.get("status", "unverified_seed"),
        "blank_end_convention": None,
        "storage_markers_interpreted": False,
    }


def score_deltas(cards):
    """Only subtract two observed cumulative totals for the same color.

    An initial zero, blank ends, and missing earlier cards are never invented.
    """
    claims = []
    for color in ("red", "yellow"):
        ordered = sorted(
            [c for c in cards if c["color"] == color and c["end_number"] is not None],
            key=lambda c: c["end_number"],
        )
        for previous, current in zip(ordered, ordered[1:], strict=False):
            points = current["cumulative_points"] - previous["cumulative_points"]
            if current["end_number"] <= previous["end_number"] or not 1 <= points <= 8:
                continue
            claims.append(
                {
                    "official_number": current["end_number"],
                    "color": color,
                    "points": points,
                    "previous_scoring_end": previous["end_number"],
                    "acceptance": "proposed",
                    "method": "difference_of_observed_cumulative_columns",
                    "requires_complete_card_read": True,
                }
            )
    return claims


SCORE_CLAIMS_VERSION = "pinned-repeated-board-score-deltas-v2"


def persistent_board_claims(observations, minimum_frames=3, maximum_gap_seconds=90):
    from hogline.reconstruction.board_events import board_timeline

    timeline = board_timeline(
        observations, minimum_frames=minimum_frames, maximum_gap_seconds=maximum_gap_seconds
    )
    by_id = {o["id"]: o for o in observations if o.get("id")}
    output = []
    for epoch in timeline["epochs"]:
        cards = [{**c, "color": epoch["color"]} for c in epoch["cards"]]
        for claim in score_deltas(cards):
            document = {
                **claim,
                "version": SCORE_CLAIMS_VERSION,
                "source_id": epoch["source_id"],
                "source_metadata_sha256": epoch["source_metadata_sha256"],
                "model_version": epoch["model_version"],
                "board_epoch_id": epoch["id"],
                "evidence_ids": epoch["evidence_ids"],
                "observation_ids": epoch["observation_ids"],
                "youtube_links": [by_id[i].get("youtube_url") for i in epoch["observation_ids"]],
                "source_start_seconds": epoch["first"]["source_seconds"],
                "source_end_seconds": epoch["last"]["source_seconds"],
                "review_reason": "board_delta_requires_game_end_and_template_review",
            }
            document["id"] = str(uuid5(NAMESPACE_URL, digest(document)))
            output.append(document)
    return output
