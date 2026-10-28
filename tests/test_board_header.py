import copy

import cv2
import numpy as np
import pytest
from PIL import Image

from hogline.config import profile
from hogline.db import Evidence
from hogline.media.frames import EvidenceBudget, observe
from hogline.perception.board_header import register_header
from hogline.perception.board_locator import locate_board
from hogline.perception.broadcast import analyze_broadcast_frame, broadcast_context

PANEL = [0, 0, 0.375, 0.95]


def reference_board():
    rgb = np.full((1080, 1920, 3), 210, np.uint8)
    x, y, w, h = 110, 100, 324, 105
    rgb[y : y + 35, x : x + w] = [180, 25, 45]
    rgb[y + 35 : y + 70, x : x + w] = [30, 40, 100]
    rgb[y + 70 : y + h, x : x + w] = [220, 200, 30]
    centers = [46, 62.5, 79, 95.5, 111.5, 128, 144, 160, 176.5, 195.5, 216, 237.5, 259, 281, 303]
    for n, c in enumerate(centers, 1):
        (tw, th), baseline = cv2.getTextSize(str(n), cv2.FONT_HERSHEY_SIMPLEX, 0.65, 1)
        cv2.putText(
            rgb,
            str(n),
            (round(x + c - tw / 2), y + 61),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.65,
            (255, 255, 255),
            1,
            cv2.LINE_AA,
        )
    image = Image.fromarray(rgb)
    template = {
        "bounds": [x / 1920, y / 1080, (x + w) / 1920, (y + h) / 1080],
        "point_centers": [c / w for c in centers],
    }
    return image, template


@pytest.mark.parametrize("size", [(1920, 1080), (1280, 720)])
def test_registration_maps_columns_without_assuming_original_crop_position(size):
    reference, template = reference_board()
    # Move the board independently of the original seed location.
    target = Image.new("RGB", (1920, 1080), (210, 210, 210))
    target.paste(reference.crop((110, 100, 434, 205)), (210, 175))
    target = target.resize(size)
    candidate = locate_board(target, PANEL)["selected"]
    result = register_header(target, candidate, reference, template, PANEL)
    assert result["status"] == "proposed"
    left, _, right, _ = candidate["normalized_bounds"]
    projected = [(left + c * (right - left)) * size[0] for c in result["point_centers"]]
    expected = [(210 + c * 324) / 1920 * size[0] for c in template["point_centers"]]
    assert projected == pytest.approx(expected, abs=1.5)
    assert result["template"]["bounds"] == candidate["normalized_bounds"]
    assert result["calibrated_probability"] is None
    assert result["template"]["status"] == "experimental_registered_header"


def test_partial_header_obstruction_does_not_pass_on_good_rest_of_header():
    reference, template = reference_board()
    target = reference.copy()
    target.paste(Image.new("RGB", (60, 30), (120, 120, 120)), (170, 138))
    result = register_header(target, locate_board(target, PANEL)["selected"], reference, template, PANEL)
    assert result["status"] == "review_required"
    assert result["point_centers"] is None and result["template"] is None
    assert min(result["regional_correlations"]) < 0.72


def test_untextured_or_wrong_header_does_not_create_columns():
    reference, template = reference_board()
    target = reference.copy()
    target.paste(Image.new("RGB", (324, 35), (30, 40, 100)), (110, 135))
    result = register_header(target, locate_board(target, PANEL)["selected"], reference, template, PANEL)
    assert result["status"] != "proposed"
    # Same colored bands and many glyphs, but not the reference point-number sequence.
    rgb = np.asarray(target).copy()
    for x in range(155, 420, 17):
        cv2.putText(rgb, "8", (x, 161), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (255, 255, 255), 1, cv2.LINE_AA)
    target = Image.fromarray(rgb)
    result = register_header(target, locate_board(target, PANEL)["selected"], reference, template, PANEL)
    assert result["status"] != "proposed"


def test_reference_arrival_changes_version_and_preserves_prior_observation(db, sources):
    reference, template = reference_board()
    layout = copy.deepcopy(profile())
    layout["broadcast"]["clock"] = {}
    layout["broadcast"]["scoreboards"] = {}
    template["layout_reference"] = {
        "source_key": f"youtube:{sources[0]['platform_id']}",
        "source_seconds": 300,
        "header_bounds": [0.11, 0.33, 0.99, 0.66],
    }
    layout["broadcast"]["automatic_board_header"] = {"reference": template}

    class Reader:
        available = True

        def read(self, *args, **kwargs):
            raise AssertionError("Empty colored rows have no cards to OCR")

    doc, _ = observe(
        reference, {"pts": 10, "time_base_num": 1, "time_base_den": 1}, sources[1], db.settings.data_dir
    )
    with db.session() as session:
        frame = Evidence(source_id=sources[1]["id"], evidence_key=doc["evidence_key"], document=doc)
        session.add(frame)
        session.flush()
        before = analyze_broadcast_frame(
            session,
            frame,
            db.settings.data_dir,
            layout,
            Reader(),
            EvidenceBudget(db.settings.data_dir, 100, 2048),
        )
        assert before["scoreboard"]["missing_reason"] == "retained_header_reference_unavailable"
        reference_doc, _ = observe(
            reference, {"pts": 300, "time_base_num": 1, "time_base_den": 1}, sources[0], db.settings.data_dir
        )
        evidence = Evidence(
            source_id=sources[0]["id"], evidence_key=reference_doc["evidence_key"], document=reference_doc
        )
        session.add(evidence)
        session.flush()
        after = analyze_broadcast_frame(
            session,
            frame,
            db.settings.data_dir,
            layout,
            Reader(),
            EvidenceBudget(db.settings.data_dir, 100, 2048),
        )
        assert after["model_version"] != before["model_version"] and after["id"] != before["id"]
        assert after["header_reference"]["evidence_id"] == evidence.id
        assert after["scoreboard"]["header_registration"]["status"] == "proposed"
        assert after["scoreboard"]["status"] == "observed" and after["scoreboard"]["cards"] == []
        assert broadcast_context(session, db.settings.data_dir, layout)["version"] == after["model_version"]


def test_registered_board_reads_card_at_its_measured_point_column(db, sources):
    reference, template = reference_board()
    image = reference.copy()
    # End card 3 above cumulative point 4; the storage area stays outside columns.
    image.paste(Image.new("RGB", (12, 29), "white"), (200, 103))
    layout = copy.deepcopy(profile())
    layout["broadcast"]["clock"] = {}
    layout["broadcast"]["scoreboards"] = {}

    class Reader:
        available = True

        def read(self, image, **kwargs):
            return {"text": "3", "engine_confidence": 80}

    context = {
        "reference": reference,
        "reference_template": template,
        "reference_manifest": {"image_sha256": "synthetic-reference"},
        "version": "synthetic-header-v1",
    }
    doc, _ = observe(
        image, {"pts": 3000, "time_base_num": 1, "time_base_den": 10}, sources[1], db.settings.data_dir
    )
    with db.session() as session:
        frame = Evidence(source_id=sources[1]["id"], evidence_key=doc["evidence_key"], document=doc)
        session.add(frame)
        session.flush()
        result = analyze_broadcast_frame(
            session,
            frame,
            db.settings.data_dir,
            layout,
            Reader(),
            EvidenceBudget(db.settings.data_dir, 100, 2048),
            context=context,
        )
        cards = result["scoreboard"]["cards"]
        assert [(c["color"], c["end_number"], c["cumulative_points"]) for c in cards] == [("red", 3, 4)]
        assert result["youtube_url"].endswith("&t=300s") and result["acceptance"] == "proposed"
        assert result["scoreboard"]["blank_end_convention"] is None
