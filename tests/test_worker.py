import json
import subprocess
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from PIL import Image
from sqlalchemy import func, select

from hogline.adapters.sources import import_local
from hogline.db import Evidence, Job, Scan
from hogline.domain.catalog import catalog, save_game
from hogline.domain.models import Game, GameEdit, ScanRequest
from hogline.media.frames import capture, observe
from hogline.orchestration import worker
from hogline.reconstruction.activity import classify, propose


def fake_capture(_, seconds):
    image = Image.new("RGB", (160, 90), (int(seconds * 10) % 256, 100, 80))
    return image, {"pts": int(seconds * 1000), "time_base_num": 1, "time_base_den": 1000}


@pytest.fixture
def fake_media(monkeypatch):
    monkeypatch.setattr(worker, "resolve_media", lambda s: {"uri": "fixture-media", "format_id": "fixture"})
    monkeypatch.setattr(worker, "capture", fake_capture)


def test_repeat_enqueue_and_retry_keep_one_logical_output(db, sources, fake_media, monkeypatch):
    req = ScanRequest(source_id=sources[0]["id"], start_seconds=100, end_seconds=130, interval_seconds=10)
    first = worker.enqueue(db, req)
    assert worker.enqueue(db, req)["id"] == first["id"]
    calls = []

    def flaky(media, seconds):
        calls.append(seconds)
        if seconds == 110:
            raise ValueError("Injected interruption")
        return fake_capture(media, seconds)

    monkeypatch.setattr(worker, "capture", flaky)
    assert worker.run_one(db)["state"] == "failed"
    assert len(list((db.settings.data_dir / "cache").glob("*.json"))) == 1
    worker.retry(db, first["id"])
    monkeypatch.setattr(worker, "capture", fake_capture)
    assert worker.run_one(db)["state"] == "succeeded"
    assert worker.run_one(db) is None
    with db.session() as session:
        assert session.scalar(select(func.count()).select_from(Scan)) == 1
        assert session.scalar(select(func.count()).select_from(Evidence)) == 3
        assert session.get(Job, first["id"]).document["cache_hits"] == 1


def test_new_scan_revision_protects_human_corrections(db, sources, fake_media):
    req = ScanRequest(source_id=sources[0]["id"], start_seconds=100, end_seconds=140, interval_seconds=10)
    worker.enqueue(db, req)
    assert worker.run_one(db)["state"] == "succeeded"
    existing = catalog(db)["games"]
    assert existing
    raw = existing[0]
    raw.pop("revision")
    for span in raw["spans"]:
        span.pop("youtube_url")
    raw["label"], raw["acceptance"] = "Human corrected", "human_confirmed"
    game = Game.model_validate(raw)
    save_game(db, GameEdit(expected_revision=1, game=game, author="Reviewer", reason="Checked original"))
    worker.enqueue(db, req.model_copy(update={"interval_seconds": 5}))
    assert worker.run_one(db)["state"] == "succeeded"
    games = catalog(db)["games"]
    assert len(games) == 1 and games[0]["label"] == "Human corrected" and games[0]["revision"] == 2


def test_expired_lease_can_resume_but_old_worker_cannot_publish(db, sources):
    queued = worker.enqueue(db, ScanRequest(source_id=sources[0]["id"], end_seconds=10))
    first = worker.claim(db)
    assert worker.claim(db) is None
    with db.session() as session:
        session.get(Job, queued["id"]).lease_until = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    second = worker.claim(db)
    assert first["lease_token"] != second["lease_token"]
    with pytest.raises(ValueError, match="reclaimed"):
        worker.update_owned(db, queued["id"], first["lease_token"], progress=1)


def test_real_decoder_records_original_pts_and_cache_integrity(db, video):
    source = import_local(db, str(video), 5)
    image, timing = capture(str(video), 1.2)
    assert timing["pts"] * timing["time_base_num"] / timing["time_base_den"] == pytest.approx(1.2, abs=0.001)
    doc, _ = observe(image, timing, source, db.settings.data_dir)
    assert doc["source_seconds"] == pytest.approx(1.2)
    req = ScanRequest(source_id=source["id"], end_seconds=3, interval_seconds=1)
    worker.enqueue(db, req)
    assert worker.run_one(db)["state"] == "succeeded"
    cache = next((db.settings.data_dir / "cache").glob("*.json"))
    manifest = json.loads(cache.read_text())
    (db.settings.data_dir / manifest["image"]["path"]).write_bytes(b"corrupt")
    assert worker.cached_frame(db, source, manifest["source_seconds"])[0] is None


def test_advertisement_is_not_an_activity_view():
    panels = {n: np.zeros((10, 10)) + 100 for n in ["house_top", "house_bottom", "perspective_left"]}
    assert classify(panels, panels)["state"] == "quiet_candidate"
    assert classify(None, panels)["state"] == "unknown"
    samples = [
        {"state": "activity_candidate", "seconds": s, "evidence_id": str(s)} for s in [10, 20, 600, 610]
    ]
    assert len(propose(samples, 0, 650, 10)) == 2


def test_nonzero_media_start_is_not_added_to_source_time(db, video, tmp_path):
    shifted = tmp_path / "nonzero-start.mp4"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(video), "-c", "copy", "-output_ts_offset", "7", str(shifted)],
        check=True,
    )
    source = import_local(db, str(shifted), 5)
    assert source["probe"]["start_pts"] > 0
    assert source["duration_seconds"] == pytest.approx(3)
    image, timing = capture(str(shifted), 1.2)
    assert timing["pts"] * timing["time_base_num"] / timing["time_base_den"] == pytest.approx(8.2)
    doc, _ = observe(image, timing, source, db.settings.data_dir)
    assert doc["source_seconds"] == pytest.approx(1.2)
