"""Traditional club boards: end-card labels and cumulative columns are distinct."""

import re
from uuid import NAMESPACE_URL, uuid5

import cv2
import numpy as np

from hogline.adapters.sources import digest
from hogline.perception.ocr import normalized_crop


def read_board(image, template, reader):
    board = normalized_crop(image, template["bounds"])
    centers = template["point_centers"]
    cards, unreadable = [], []
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
            hsv = cv2.cvtColor(np.asarray(cell.convert("RGB")), cv2.COLOR_RGB2HSV)
            white = cv2.inRange(hsv, (0, 0, 140), (179, 80, 255))
            occupancy = float(np.mean(white > 0))
            if occupancy < 0.2:
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
                "white_card_fraction": occupancy,
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


def persistent_board_claims(observations, minimum_frames=3, maximum_gap_seconds=90):
    # Each row has its own cumulative ledger. An occluded yellow row must not
    # erase readable red evidence, and no vote spans an interruption or reset.
    states = []
    current = {}
    for observation in sorted(observations, key=lambda o: (o["source_id"], o["source_seconds"])):
        board = observation.get("scoreboard", {})
        for color in ("red", "yellow"):
            row_key = (observation["source_id"], color)
            cards = [c for c in board.get("cards", []) if c["color"] == color]
            invalid = any(s["color"] == color for s in board.get("unreadable_slots", []))
            invalid |= any(c["end_number"] in board.get("conflicting_end_numbers", []) for c in cards)
            if not cards or invalid or any(c["end_number"] is None for c in cards):
                current.pop(row_key, None)
                continue
            key = (
                observation["source_id"],
                color,
                tuple(sorted((c["end_number"], c["cumulative_points"]) for c in cards)),
            )
            group = current.get(row_key)
            if (
                group is None
                or group["key"] != key
                or observation["source_seconds"] - group["times"][-1] > maximum_gap_seconds
            ):
                group = {"key": key, "cards": cards, "evidence": {}, "times": []}
                current[row_key] = group
                states.append(group)
            group["evidence"][observation["evidence_id"]] = observation.get("youtube_url")
            group["times"].append(observation["source_seconds"])
    output = []
    for state in states:
        source_id = state["key"][0]
        if len(state["evidence"]) < minimum_frames or len(set(state["times"])) < minimum_frames:
            continue
        for claim in score_deltas(state["cards"]):
            document = {
                **claim,
                "source_id": source_id,
                "evidence_ids": list(state["evidence"]),
                "youtube_links": list(state["evidence"].values()),
                "source_start_seconds": min(state["times"]),
                "source_end_seconds": max(state["times"]),
                "review_reason": "board_delta_requires_game_end_and_template_review",
            }
            document["id"] = str(uuid5(NAMESPACE_URL, digest(document)))
            output.append(document)
    return output
