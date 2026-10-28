"""Locate red/header/yellow board geometry without assigning point columns."""

import hashlib
from uuid import NAMESPACE_URL, uuid5

import cv2
import numpy as np
from PIL import Image
from sqlalchemy import select

from hogline.db import FrameObservation
from hogline.perception.ocr import normalized_crop

VERSION = "traditional-board-color-bands-v1"


def locate_board(image, panel_bounds):
    rgb = np.asarray(image.convert("RGB"))
    height, width = rgb.shape[:2]
    left, top, right, bottom = panel_bounds
    x0, y0 = round(left * width), round(top * height)
    x1 = round(right * width)
    # The Wausau board is on the end wall. Exclude the foreground ice and overlays.
    y1 = round((top + (bottom - top) * 0.45) * height)
    patch = rgb[y0:y1, x0:x1]
    if patch.size == 0:
        return {"status": "unavailable", "missing_reason": "empty_board_search_panel", "candidates": []}
    hsv = cv2.cvtColor(patch, cv2.COLOR_RGB2HSV)
    pw, ph = patch.shape[1], patch.shape[0]
    yellow = cv2.inRange(hsv, (17, 90, 70), (42, 255, 255))
    red = cv2.inRange(hsv, (0, 85, 50), (13, 255, 255)) | cv2.inRange(hsv, (165, 85, 50), (179, 255, 255))
    blue = cv2.inRange(hsv, (90, 45, 15), (145, 255, 255))

    def bands(mask):
        closed = cv2.morphologyEx(
            mask, cv2.MORPH_CLOSE, np.ones((max(1, round(ph * 0.005)), max(3, round(pw * 0.025))), np.uint8)
        )
        _, _, stats, _ = cv2.connectedComponentsWithStats(closed)
        return [
            tuple(map(int, s))
            for s in stats[1:]
            if pw * 0.2 <= s[2] <= pw * 0.95
            and ph * 0.015 <= s[3] <= ph * 0.2
            and 4 <= s[2] / s[3] <= 18
            and s[4] / (s[2] * s[3]) >= 0.5
        ]

    candidates = []
    for yx, yy, yw, yh, ya in bands(yellow):
        for rx, ry, rw, rh, ra in bands(red):
            overlap = max(0, min(yx + yw, rx + rw) - max(yx, rx))
            gap = yy - (ry + rh)
            if overlap / max(yw, rw) < 0.75 or not 0.4 * min(yh, rh) <= gap <= 2 * max(yh, rh):
                continue
            if not 0.45 <= yh / rh <= 2.2:
                continue
            hx0, hx1 = max(yx, rx), min(yx + yw, rx + rw)
            header = blue[ry + rh : yy, hx0:hx1]
            support = float(np.mean(header > 0)) if header.size else 0
            if support < 0.3:
                continue
            bx, by = min(yx, rx), ry
            bw, bh = max(yx + yw, rx + rw) - bx, yy + yh - by
            score = support * overlap / max(yw, rw) * min(ya / (yw * yh), ra / (rw * rh))
            candidates.append(
                {
                    "bounds_px": [x0 + bx, y0 + by, bw, bh],
                    "normalized_bounds": [
                        (x0 + bx) / width,
                        (y0 + by) / height,
                        (x0 + bx + bw) / width,
                        (y0 + by + bh) / height,
                    ],
                    "row_bands": {
                        "red": [(ry - by) / bh, (ry + rh - by) / bh],
                        "yellow": [(yy - by) / bh, (yy + yh - by) / bh],
                    },
                    "header_bounds": [(hx0 - bx) / bw, rh / bh, (hx1 - bx) / bw, (yy - by) / bh],
                    "blue_header_fraction": support,
                    "geometry_score": float(score),
                    "score_kind": "uncalibrated_color_band_agreement",
                    "acceptance": "proposed",
                }
            )
    candidates.sort(key=lambda c: (-c["geometry_score"], c["bounds_px"]))
    ambiguous = (
        len(candidates) > 1 and candidates[1]["geometry_score"] >= candidates[0]["geometry_score"] * 0.8
    )
    return {
        "method": VERSION,
        "status": "ambiguous" if ambiguous else "proposed" if candidates else "unavailable",
        "candidates": candidates[:3],
        "candidate_count": len(candidates),
        "selected": candidates[0] if candidates and not ambiguous else None,
        "search_normalized_bounds": [left, top, right, (top + (bottom - top) * 0.45)],
        "missing_reason": "multiple_plausible_boards"
        if ambiguous
        else None
        if candidates
        else "colored_board_bands_not_found",
        "point_columns": None,
        "blank_end_convention": None,
        "limitation": (
            "image-space board proposal; header columns, storage markers and card meanings unverified"
        ),
    }


def analyze_board_location(session, evidence, root):
    frame = evidence.document
    if frame.get("acquisition_tier") == "coarse":
        return None
    identifier = str(uuid5(NAMESPACE_URL, f"{evidence.id}:{VERSION}"))
    existing = session.get(FrameObservation, identifier)
    if existing:
        return existing.document
    asset = frame["image"]
    path = root / asset["path"]
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != asset["sha256"]:
        raise ValueError("Cannot locate a board in missing/corrupt evidence")
    image = Image.open(path).convert("RGB")
    location = locate_board(image, frame["crops"]["perspective_left"]["normalized_bounds"])
    crops = []
    for candidate in location["candidates"]:
        crop = normalized_crop(image, candidate["normalized_bounds"])
        crops.append(
            {
                "normalized_bounds": candidate["normalized_bounds"],
                "width": crop.width,
                "height": crop.height,
                "rgb_sha256": hashlib.sha256(crop.tobytes()).hexdigest(),
            }
        )
    document = {
        "id": identifier,
        "kind": "board_location_observation",
        "model_version": VERSION,
        "evidence_id": evidence.id,
        "source_id": evidence.source_id,
        "source_metadata_sha256": frame.get("source_metadata_sha256"),
        "source_image_sha256": asset["sha256"],
        "source_seconds": frame["source_seconds"],
        "pts": frame["pts"],
        "time_base_num": frame["time_base_num"],
        "time_base_den": frame["time_base_den"],
        "youtube_url": frame.get("youtube_url"),
        "location": location,
        "crops": crops,
        "acceptance": "proposed",
        "score": None,
    }
    session.add(
        FrameObservation(id=identifier, evidence_id=evidence.id, model_version=VERSION, document=document)
    )
    return document


def board_locations_in_session(session):
    return [
        r.document
        for r in session.scalars(select(FrameObservation).where(FrameObservation.model_version == VERSION))
    ]
