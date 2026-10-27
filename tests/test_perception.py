import cv2
import numpy as np
from PIL import Image
from sqlalchemy import func, select

from hogline.db import FrameObservation
from hogline.domain.catalog import catalog
from hogline.domain.models import ScanRequest
from hogline.orchestration import worker
from hogline.perception.pipeline import analyze_cached
from hogline.perception.stones import HSVHandleDetector


def test_color_shape_baseline_rejects_house_rings_and_red_border():
    image = np.full((480, 480, 3), 220, dtype=np.uint8)
    cv2.circle(image, (240, 240), 190, (45, 55, 180), thickness=40)
    cv2.circle(image, (240, 240), 60, (30, 150, 70), thickness=-1)
    cv2.rectangle(image, (0, 0), (479, 479), (150, 40, 40), thickness=6)
    cv2.circle(image, (200, 230), 12, (210, 50, 40), thickness=-1)
    cv2.circle(image, (280, 260), 12, (230, 210, 30), thickness=-1)
    detections = HSVHandleDetector().detect(image)
    assert len(detections) == 2
    assert {d["color"] for d in detections} == {"red", "yellow"}
    assert all(d["acceptance"] == "proposed" for d in detections)
    assert all(d["object_kind"] == "colored_handle_candidate" for d in detections)


def test_reanalyze_cached_frames_without_media_fetch_preserves_observations(db, sources, monkeypatch):
    monkeypatch.setattr(worker, "resolve_media", lambda s: {"uri": "fixture", "format_id": "fixture"})
    monkeypatch.setattr(
        worker,
        "capture",
        lambda m, s: (
            Image.new("RGB", (160, 90), "white"),
            {"pts": 0, "time_base_num": 1, "time_base_den": 10},
        ),
    )
    worker.enqueue(db, ScanRequest(source_id=sources[0]["id"], end_seconds=1, interval_seconds=1))
    assert worker.run_one(db)["state"] == "succeeded"
    first = analyze_cached(db)
    second = analyze_cached(db)
    assert first[0]["id"] == second[0]["id"]
    assert first[0]["score"] is None
    with db.session() as session:
        assert session.scalar(select(func.count()).select_from(FrameObservation)) == 1
    assert len(catalog(db)["games"]) == 0


def test_evidence_budget_stops_before_writing_past_limit(db, sources, monkeypatch):
    random = np.random.default_rng(1).integers(0, 256, (512, 512, 3), dtype=np.uint8)
    monkeypatch.setattr(worker, "resolve_media", lambda s: {"uri": "fixture", "format_id": "fixture"})
    monkeypatch.setattr(
        worker,
        "capture",
        lambda m, s: (Image.fromarray(random), {"pts": 0, "time_base_num": 1, "time_base_den": 10}),
    )
    worker.enqueue(
        db, ScanRequest(source_id=sources[0]["id"], end_seconds=1, interval_seconds=1, max_evidence_mb=1)
    )
    result = worker.run_one(db)
    assert result["state"] == "failed" and "budget" in result["error"]
    written = sum(p.stat().st_size for p in (db.settings.data_dir / "evidence").rglob("*.png"))
    assert written <= 1024**2
    assert catalog(db)["evidence"] == []
