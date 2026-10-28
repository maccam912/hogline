import hashlib
import json

import numpy as np
import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw
from sqlalchemy import func, select

from hogline.adapters.sources import import_local
from hogline.analytics.exports import snapshot
from hogline.db import AnalysisWindow, Evidence, Job, Scan
from hogline.domain.catalog import catalog
from hogline.orchestration import windows, worker
from hogline.perception.temporal import persistent_configurations
from hogline.perception.visibility import foreground_regions, visibility_observation
from hogline.review.app import create_app


def row(index, *, x=50, color="red", clipped=False, count=1, motion=0, seconds=None):
    return {
        "view": "house_top",
        "sample_index": index,
        "source_seconds": index * 0.5 if seconds is None else seconds,
        "requested_interval_seconds": 0.5,
        "crop_width": 200,
        "crop_height": 300,
        "motion_mean_abs": motion,
        "foreground_regions": [],
        "ring_reference": {"status": "candidate"},
        "detections": [
            {"center_px": [x + n * 20, 50], "color": color, "boundary_clipped": clipped} for n in range(count)
        ],
    }


def test_persistence_requires_positions_colors_visibility_motion_and_time():
    assert len(persistent_configurations([row(i) for i in range(4)])) == 1
    assert persistent_configurations([row(i) for i in range(3)]) == []
    assert persistent_configurations([row(i, count=0) for i in range(10)]) == []
    assert persistent_configurations([row(i, clipped=True) for i in range(10)]) == []
    assert persistent_configurations([row(i, motion=10) for i in range(10)]) == []
    obstructed = [
        {**row(i), "foreground_regions": [{"bbox_px": [0, 0, 50, 50], "area_px2": 2000}]} for i in range(8)
    ]
    assert persistent_configurations(obstructed) == []
    no_reference = [{**row(i), "ring_reference": {"status": "unavailable"}} for i in range(8)]
    assert persistent_configurations(no_reference) == []
    assert persistent_configurations([row(i, x=50 + i) for i in range(4)]) == []  # Slow drift.
    assert persistent_configurations([row(i, color="red" if i % 2 else "yellow") for i in range(8)]) == []
    assert persistent_configurations([row(0), row(1), row(2), row(3, seconds=6)]) == []
    changed = persistent_configurations([row(i, x=50 if i < 4 else 90) for i in range(8)])
    assert [c["sample_indices"] for c in changed] == [[0, 1, 2, 3], [4, 5, 6, 7]]
    assert all(c["score"] is None and c["acceptance"] == "proposed" for c in changed)


@pytest.fixture
def fake_window(monkeypatch):
    image = Image.new("RGB", (1280, 720), "white")
    draw = ImageDraw.Draw(image)
    draw.ellipse((500, 20, 780, 300), outline=(45, 55, 180), width=25)
    draw.ellipse((536, 126, 550, 140), fill=(225, 20, 20))
    draw.ellipse((577, 170, 591, 184), fill=(240, 205, 20))
    draw.ellipse((647, 197, 653, 203), fill=(225, 20, 20))
    calls = []
    monkeypatch.setattr(windows, "resolve_media", lambda s: {"uri": "fixture", "format_id": "fixture"})

    def decode(media, start, end, interval, **kwargs):
        calls.append((start, end))
        index = 0
        while start + index * interval < end:
            seconds = start + index * interval
            yield image.copy(), {"pts": round(seconds * 1000), "time_base_num": 1, "time_base_den": 1000}
            index += 1

    monkeypatch.setattr(windows, "decode_window", decode)
    return calls


def test_dense_window_has_columnar_rows_bounded_anchors_and_no_games(db, sources, fake_window):
    request = windows.WindowRequest(source_id=sources[0]["id"], start_seconds=100, end_seconds=105)
    job = windows.enqueue_window(db, request)
    assert windows.enqueue_window(db, request)["id"] == job["id"]
    assert worker.run_one(db)["state"] == "succeeded"
    assert worker.run_one(db) is None
    data = catalog(db)
    assert not data["games"] and not data["scans"]
    window = data["analysis_windows"][0]
    assert window["sample_count"] == 10
    assert window["coverage_kind"] == "discrete_dense_samples"
    assert len(data["evidence"]) == 3
    assert window["candidates"][0]["candidate_count"] == 2
    assert set(window["candidates"][0]["evidence_ids"]) < set(window["anchor_evidence_ids"])
    assert window["candidates"][0]["youtube_url"].endswith("&t=100s")
    artifact = db.settings.data_dir / window["parquet"]["path"]
    assert hashlib.sha256(artifact.read_bytes()).hexdigest() == window["parquet"]["sha256"]
    table = pq.read_table(artifact)
    assert table.num_rows == 20
    rejected = table.to_pylist()[0]["rejected_candidates"]
    assert len(rejected) == 1
    assert rejected[0]["rejection_reasons"] == ["colored_area_below_crop_prior"]
    assert table.schema.metadata[b"hogline.schema_version"] == b"hogline-dense-observations-v4"
    assert table.schema.field("pts").type.bit_width == 64
    assert table.to_pylist()[0]["detections"][0]["color"] == "red"
    manifest = json.loads(table.schema.metadata[b"hogline.input_manifest"])
    assert manifest["source_hash"] == sources[0]["metadata_sha256"]
    assert "fixture" not in json.dumps(manifest)
    assert fake_window == [(100, 105)]
    release = snapshot(db, "exploratory")
    assert release["analysis_windows"][0]["id"] == window["id"]
    assert release["lineage_gaps"] == {"unpinned_evidence_ids": [], "missing_metadata_snapshots": []}
    with TestClient(create_app(db.settings)) as client:
        path = f"/api/windows/{window['id']}/observations.parquet"
        assert client.get(path).content == artifact.read_bytes()
        artifact.write_bytes(b"corrupt")
        assert client.get(path).status_code == 404


def test_dense_decoder_failure_publishes_no_partial_sql_output(db, sources, monkeypatch):
    monkeypatch.setattr(windows, "resolve_media", lambda s: {"uri": "fixture"})

    def incomplete(*args, **kwargs):
        yield Image.new("RGB", (320, 180), "white"), {"pts": 0, "time_base_num": 1, "time_base_den": 1000}
        raise ValueError("Injected decoder interruption")

    monkeypatch.setattr(windows, "decode_window", incomplete)
    windows.enqueue_window(
        db, windows.WindowRequest(source_id=sources[0]["id"], start_seconds=0, end_seconds=2)
    )
    assert worker.run_one(db)["state"] == "failed"
    with db.session() as session:
        for model in (Evidence, AnalysisWindow, Scan):
            assert session.scalar(select(func.count()).select_from(model)) == 0
        assert session.scalar(select(Job)).document["error"] == "Injected decoder interruption"


def test_real_window_worker_decodes_the_declared_source_window(db, video):
    source = import_local(db, str(video), 5)
    request = windows.WindowRequest(source_id=source["id"], start_seconds=0.4, end_seconds=2.5, max_frames=5)
    windows.enqueue_window(db, request)
    assert worker.run_one(db)["state"] == "succeeded"
    window = catalog(db)["analysis_windows"][0]
    rows = pq.read_table(db.settings.data_dir / window["parquet"]["path"]).to_pylist()
    assert [r["source_seconds"] for r in rows if r["view"] == "house_top"] == pytest.approx(
        [0.4, 0.9, 1.4, 1.9, 2.4]
    )


def test_window_api_rejects_out_of_bounds_before_enqueue(db, sources):
    with TestClient(create_app(db.settings)) as client:
        body = {"source_id": sources[0]["id"], "start_seconds": 0, "end_seconds": 61}
        assert client.post("/api/windows", json=body).status_code == 422
        body["end_seconds"] = 2
        assert client.post("/api/windows", json=body).json()["kind"] == "dense_window"


@pytest.mark.parametrize("size", [320, 480])
def test_dark_foreground_gate_scales_and_does_not_claim_people(size):
    image = Image.new("RGB", (size, size), "white")
    draw = ImageDraw.Draw(image)
    draw.ellipse((5, 5, size - 5, size - 5), outline=(20, 20, 130), width=size // 8)
    draw.ellipse((size // 3, size // 3, 2 * size // 3, 2 * size // 3), fill=(30, 140, 70))
    assert foreground_regions(np.asarray(image)) == []
    draw.rectangle((size // 8, size // 2, size // 3, 3 * size // 4), fill=(30, 30, 35))
    regions = foreground_regions(np.asarray(image))
    assert any(r["kind"] == "large_dark_region" and r["area_px2"] > size**2 * 0.01 for r in regions)


@pytest.mark.parametrize("size", [320, 480])
def test_blue_ring_reference_catches_bright_gray_occlusion_but_not_small_granite(size):
    image = Image.new("RGB", (size, size), "white")
    draw = ImageDraw.Draw(image)
    draw.ellipse(
        (size * 0.05, size * 0.05, size * 0.95, size * 0.95), outline=(45, 55, 180), width=round(size * 0.15)
    )
    draw.ellipse((size * 0.08, size * 0.47, size * 0.14, size * 0.53), fill=(140, 140, 140))
    measured = visibility_observation(np.asarray(image))
    assert measured["ring_reference"]["status"] == "candidate"
    assert measured["foreground_regions"] == []
    draw.rectangle((size * 0.07, size * 0.36, size * 0.22, size * 0.57), fill=(140, 140, 140))
    measured = visibility_observation(np.asarray(image))
    assert measured["foreground_regions"]
    assert all(r["kind"] == "blue_ring_obstruction" for r in measured["foreground_regions"])
    # An unchanged colored object on this static obstruction must not become a configuration.
    assert persistent_configurations([{**row(i), **measured} for i in range(8)]) == []


def test_explicit_reanalysis_preserves_old_logical_window(db, sources, fake_window):
    request = windows.WindowRequest(source_id=sources[0]["id"], start_seconds=100, end_seconds=105)
    first = windows.enqueue_window(db, request)
    assert worker.run_one(db)["state"] == "succeeded"
    second = windows.enqueue_window(db, request.model_copy(update={"run_nonce": "explicit-reanalysis"}))
    assert second["id"] != first["id"]
    assert worker.run_one(db)["state"] == "succeeded"
    with db.session() as session:
        assert session.scalar(select(func.count()).select_from(AnalysisWindow)) == 2


@pytest.mark.parametrize(
    "seconds,expected", [([100, 100.7, 102.2, 104.8], [100, 102.2, 104.8]), ([100, 104.8], [100, 104.8])]
)
def test_temporal_anchors_use_real_nearest_midpoint_even_without_house_candidates(
    db, sources, monkeypatch, seconds, expected
):
    monkeypatch.setattr(windows, "resolve_media", lambda source: {"uri": "fixture"})

    def decode(*args, **kwargs):
        for value in seconds:
            yield (
                Image.new("RGB", (640, 360), "white"),
                {"pts": round(value * 1000), "time_base_num": 1, "time_base_den": 1000},
            )

    monkeypatch.setattr(windows, "decode_window", decode)
    windows.enqueue_window(
        db, windows.WindowRequest(source_id=sources[0]["id"], start_seconds=100, end_seconds=105)
    )
    assert worker.run_one(db)["state"] == "succeeded"
    data = catalog(db)
    window = data["analysis_windows"][0]
    evidence = {e["id"]: e for e in data["evidence"]}
    anchors = [evidence[i] for i in window["temporal_anchor_evidence_ids"]]
    assert [e["source_seconds"] for e in anchors] == expected
    assert [e["pts"] for e in anchors] == [round(t * 1000) for t in expected]
    assert not window["candidates"]
    assert window["retention_version"] == windows.RETENTION_VERSION
    assert len(window["anchor_evidence_ids"]) == len(expected)
