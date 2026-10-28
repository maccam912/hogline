import json

import cv2
import numpy as np
from PIL import Image

from hogline.config import profile
from hogline.db import Evidence, Source
from hogline.domain.models import ScanRequest
from hogline.media.frames import EvidenceBudget, observe
from hogline.orchestration import worker
from hogline.perception.broadcast import analyze_broadcast_frame, template_applies
from hogline.perception.house import house_ring
from hogline.perception.layout import board_alignment, reference_image


def test_house_fit_scales_with_resolution_without_claiming_metric_calibration():
    for size in (320, 480):
        image = np.full((size, size, 3), 230, dtype=np.uint8)
        cv2.circle(image, (size // 2, size // 2), int(size * 0.45), (40, 50, 180), thickness=int(size * 0.12))
        result = house_ring(image)
        assert result["status"] == "candidate"
        assert abs(result["center_px"][0] / size - 0.5) < 0.02
        assert result["metric_calibration"] is None
    assert house_ring(np.zeros((320, 320, 3), dtype=np.uint8))["status"] == "unavailable"


def test_crop_revisions_reuse_retained_original_without_acquisition(db, sources, monkeypatch, tmp_path):
    calls = []

    def capture(media, seconds):
        calls.append(seconds)
        return Image.new("RGB", (160, 90), "white"), {
            "pts": int(seconds * 10),
            "time_base_num": 1,
            "time_base_den": 10,
        }

    monkeypatch.setattr(worker, "resolve_media", lambda s: {"uri": "fixture", "format_id": "fixture"})
    monkeypatch.setattr(worker, "capture", capture)
    request = ScanRequest(source_id=sources[0]["id"], end_seconds=1, interval_seconds=1)
    worker.enqueue(db, request)
    assert worker.run_one(db)["state"] == "succeeded"
    layout = profile()
    layout["views"]["house_top"][0] = 0.38
    path = tmp_path / "different-profile.json"
    path.write_text(json.dumps(layout))
    monkeypatch.setenv("HOGLINE_PROFILE", str(path))
    worker.enqueue(db, request)
    assert worker.run_one(db)["state"] == "succeeded"
    assert len(calls) == 1


def test_scoreboard_configuration_is_scoped_to_sheet_not_reused_globally(db, sources):
    class OCR:
        available = True

        def read(self, image, whitelist, psm, preprocessing="otsu"):
            return {"text": "03/17/2026" if "/" in whitelist else "09:57:48", "engine_confidence": 80}

    image = Image.new("RGB", (1920, 1080), "white")
    document, _ = observe(
        image, {"pts": 0, "time_base_num": 1, "time_base_den": 30}, sources[0], db.settings.data_dir
    )
    with db.session() as session:
        evidence = Evidence(
            source_id=sources[0]["id"], evidence_key="sheet5-broadcast-fixture", document=document
        )
        session.add(evidence)
        session.flush()
        result = analyze_broadcast_frame(
            session,
            evidence,
            db.settings.data_dir,
            profile(),
            OCR(),
            EvidenceBudget(db.settings.data_dir, 100, 2048),
        )
        assert result["sheet"] == 5
        assert result["scoreboard"]["status"] == "unconfigured"
        assert result["clock"]["local_date"] == "2026-03-17"


def test_board_template_requires_matching_recording_and_time():
    template = profile()["broadcast"]["scoreboards"]["4"]
    source = {"platform": "youtube", "platform_id": "es9urvYvLiE"}
    assert template_applies(template, source, 300)
    assert not template_applies(template, source, 2210)
    assert not template_applies(template, {**source, "platform_id": "AAAAAAAAAAA"}, 300)
    assert not template_applies({}, source, 300)


def test_same_sheet_new_recording_does_not_inherit_board_reader(db, sources):
    class OCR:
        available = True
        calls = 0

        def read(self, image, whitelist, psm, preprocessing="otsu"):
            self.calls += 1
            return {"text": ""}

    reader = OCR()
    image = Image.new("RGB", (1920, 1080), "white")
    document, _ = observe(
        image, {"pts": 300000, "time_base_num": 1, "time_base_den": 1000}, sources[0], db.settings.data_dir
    )
    with db.session() as session:
        row = session.get(Source, sources[0]["id"])
        row.document = {**row.document, "sheet": 4}
        evidence = Evidence(source_id=row.id, evidence_key="another-sheet4-recording", document=document)
        session.add(evidence)
        session.flush()
        result = analyze_broadcast_frame(
            session,
            evidence,
            db.settings.data_dir,
            profile(),
            reader,
            EvidenceBudget(db.settings.data_dir, 100, 2048),
        )
        assert result["sheet"] == 4
        assert result["scoreboard"]["missing_reason"] == "retained_header_reference_unavailable"
        assert reader.calls == 4  # date/time alternatives only; no board-cell OCR


def test_static_board_alignment_translates_small_shifts_and_declines_large_changes():
    image = np.full((300, 600, 3), 220, dtype=np.uint8)
    cv2.rectangle(image, (60, 105), (540, 150), (25, 35, 130), -1)
    for i in range(15):
        cv2.putText(image, str(i + 1), (65 + i * 31, 137), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
    template = {"bounds": [0.1, 0.2, 0.9, 0.7], "layout_reference": {"header_bounds": [0, 0.3, 1, 0.6]}}
    reference = Image.fromarray(image)
    shifted = Image.fromarray(
        cv2.warpAffine(image, np.float32([[1, 0, 2], [0, 1, 3]]), (600, 300), borderValue=(220, 220, 220))
    )
    result = board_alignment(shifted, reference, template)
    assert result["status"] == "translated_seed"
    assert result["translation_source_px"] == [2, 3]
    assert result["after_translation"]["header_correlation"] > 0.99
    large = Image.fromarray(cv2.warpAffine(image, np.float32([[1, 0, 50], [0, 1, 30]]), (600, 300)))
    assert board_alignment(large, reference, template)["status"] == "drift_or_occlusion"
    assert (
        board_alignment(Image.new("RGB", (600, 300), "white"), reference, template)["status"] == "unavailable"
    )


def test_missing_reference_does_not_trigger_acquisition(db):
    with db.session() as session:
        image, result = reference_image(
            session, db.settings.data_dir, profile()["broadcast"]["scoreboards"]["4"]
        )
        assert image is None
        assert result["missing_reason"] == "retained_layout_reference_missing_or_corrupt"
