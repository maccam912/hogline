import json

import pytest
from PIL import Image

from hogline.db import Source
from hogline.domain.catalog import catalog
from hogline.domain.models import ScanRequest
from hogline.orchestration import worker
from hogline.reconstruction.timeline import reconstruct_source


def test_coarse_and_evidence_caches_never_substitute_lower_resolution(db, sources, monkeypatch):
    tiers = []

    def resolve(source, max_height=1080):
        tiers.append(max_height)
        return {"uri": str(max_height), "format_id": str(max_height)}

    def capture(media, seconds):
        height = int(media)
        return Image.new("RGB", (int(height * 16 / 9), height), "white"), {
            "pts": 0,
            "time_base_num": 1,
            "time_base_den": 30,
        }

    monkeypatch.setattr(worker, "resolve_media", resolve)
    monkeypatch.setattr(worker, "capture", capture)
    base = {"source_id": sources[0]["id"], "end_seconds": 1, "interval_seconds": 1}
    worker.enqueue(db, ScanRequest(**base, acquisition_tier="coarse"))
    assert worker.run_one(db)["state"] == "succeeded"
    worker.enqueue(db, ScanRequest(**base, acquisition_tier="evidence"))
    assert worker.run_one(db)["state"] == "succeeded"
    frames = catalog(db)["evidence"]
    assert sorted(f["image"]["height"] for f in frames) == [360, 1080]
    assert tiers == [360, 1080]
    assert len(catalog(db)["games"]) == 0


def test_changed_source_manifest_does_not_reuse_old_acquisition(db, sources, monkeypatch):
    calls = []
    monkeypatch.setattr(worker, "resolve_media", lambda s: {"uri": "fixture"})

    def capture(media, seconds):
        calls.append(seconds)
        return Image.new("RGB", (640, 360), "white"), {"pts": 0, "time_base_num": 1, "time_base_den": 30}

    monkeypatch.setattr(worker, "capture", capture)
    req = ScanRequest(source_id=sources[0]["id"], end_seconds=1, interval_seconds=1)
    worker.enqueue(db, req)
    assert worker.run_one(db)["state"] == "succeeded"
    with db.session() as session:
        row = session.get(Source, req.source_id)
        row.document = {**row.document, "metadata_sha256": "changed-manifest"}
    worker.enqueue(db, req)
    assert worker.run_one(db)["state"] == "succeeded"
    assert len(calls) == 2
    assert catalog(db)["timelines"][0]["stale_scans_excluded"] == 1


def test_crop_budget_failure_retains_original_for_resume(db, sources, monkeypatch):
    import numpy as np

    image = Image.fromarray(np.random.default_rng(4).integers(0, 256, (512, 512, 3), dtype=np.uint8))
    calls = []
    monkeypatch.setattr(worker, "resolve_media", lambda s: {"uri": "fixture", "format_id": "fixture"})

    def capture(media, seconds):
        calls.append(seconds)
        return image, {"pts": 0, "time_base_num": 1, "time_base_den": 30}

    monkeypatch.setattr(worker, "capture", capture)
    req = ScanRequest(source_id=sources[0]["id"], end_seconds=1, interval_seconds=1, max_evidence_mb=1)
    worker.enqueue(db, req)
    assert worker.run_one(db)["state"] == "failed"
    paths = list((db.settings.data_dir / "cache").glob("*.json"))
    assert paths and json.loads(paths[0].read_text())["image"]["sha256"]
    worker.enqueue(db, req.model_copy(update={"max_evidence_mb": 3}))
    assert worker.run_one(db)["state"] == "succeeded"
    assert len(calls) == 1


def test_ownership_bounds_stay_inside_context():
    with pytest.raises(ValueError):
        ScanRequest(source_id="s", end_seconds=100, ownership_start_seconds=20)
    with pytest.raises(ValueError):
        ScanRequest(source_id="s", end_seconds=100, ownership_start_seconds=20, ownership_end_seconds=120)


def scan(start, end, times, owner=None):
    owner = owner or (start, end)
    return {
        "request": {
            "start_seconds": start,
            "end_seconds": end,
            "interval_seconds": 10,
            "ownership_start_seconds": owner[0],
            "ownership_end_seconds": owner[1],
        },
        "samples": [
            {
                "seconds": t,
                "requested_seconds": t,
                "owned": owner[0] <= t < owner[1],
                "state": "activity_candidate",
                "evidence_id": str(t),
            }
            for t in times
        ],
    }


def test_chunks_join_activity_without_duplicate_context_samples_or_inventing_games():
    source = {"id": "s", "sheet": 5, "duration_seconds": 300, "platform": "local"}
    evidence = {str(t): {"image": {"width": 640, "height": 360}} for t in range(0, 210, 10)}
    scans = [scan(0, 120, range(0, 120, 10), (0, 100)), scan(80, 220, range(80, 220, 10), (100, 200))]
    result = reconstruct_source(source, scans, evidence)
    assert result["unique_owned_samples"] == 20
    assert result["sparse_scanned_seconds"] == 200
    assert result["unscanned_intervals"] == [[200, 300]]
    assert len(result["activity_runs"]) == 1
    assert result["game_count"] is None
    separated = reconstruct_source(
        source, [scan(0, 40, [0, 10, 20, 30]), scan(60, 100, [60, 70, 80, 90])], evidence
    )
    assert len(separated["activity_runs"]) == 2
