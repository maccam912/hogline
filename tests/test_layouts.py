import json

import cv2
import numpy as np
from PIL import Image

from hogline.config import profile
from hogline.db import Evidence
from hogline.domain.models import ScanRequest
from hogline.media.frames import EvidenceBudget, observe
from hogline.orchestration import worker
from hogline.perception.broadcast import analyze_broadcast_frame
from hogline.perception.house import house_ring


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

        def read(self, image, whitelist, psm):
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
