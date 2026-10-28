import copy

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from hogline.analytics.exports import snapshot
from hogline.analytics.tracks import derive_window_tracks, load_track_input, validate_rows
from hogline.db import AnalysisWindow, WindowTracks
from hogline.domain.catalog import catalog
from hogline.orchestration import windows, worker
from hogline.perception.tracking import track_handles
from hogline.review.app import create_app
from tests.test_dense_analysis import fake_window  # noqa: F401


def sample(i, positions=(30,), colors=None, **kw):
    return {
        "sample_index": i,
        "view": "house_top",
        "source_seconds": i * 0.5,
        "pts": i,
        "time_base_num": 1,
        "time_base_den": 2,
        "decoded_rgb_sha256": f"image-{i}",
        "youtube_url": f"https://www.youtube.com/watch?v=AAAAAAAAAAA&t={int(i * 0.5)}s",
        "crop_width": 200,
        "crop_height": 200,
        "requested_interval_seconds": 0.5,
        "ring_reference": {"status": "candidate"},
        "foreground_regions": [],
        "detections": [
            {
                "color": (colors or ["red"] * len(positions))[n],
                "center_px": [x, 100],
                "boundary_clipped": False,
            }
            for n, x in enumerate(positions)
        ],
        **kw,
    }


def test_straight_fragments_retain_original_points_and_no_physical_identity():
    rows = [sample(i, (20 + 10 * i, 150), ("red", "yellow")) for i in range(6)]
    result = track_handles(rows, "input-sha")
    assert result == track_handles(list(reversed(rows)), "input-sha")
    assert len(result["tracks"]) == 2
    moving = next(t for t in result["tracks"] if t["color"] == "red")
    assert moving["net_displacement_px"] == moving["sampled_path_length_px"] == 50
    assert moving["motion_class"] == "position_change_observed"
    assert [p["pts"] for p in moving["points"]] == list(range(6))
    assert moving["start_seconds"] == 0 and moving["end_seconds"] == 2.5
    assert moving["physical_stone_id"] is moving["shot_id"] is moving["speed_meters_per_second"] is None
    assert moving["points"][-1]["youtube_url"].endswith("&t=2s")
    assert moving["id"] != track_handles(rows, "different-input")["tracks"][0]["id"]


@pytest.mark.parametrize("reason", ["foreground", "ring", "missing", "clipped"])
def test_obstructions_absence_and_clipping_never_bridge_identity(reason):
    rows = [sample(i) for i in range(7)]
    if reason == "foreground":
        rows[3]["foreground_regions"] = [{"kind": "person"}]
    if reason == "ring":
        rows[3]["ring_reference"] = {"status": "unavailable"}
    if reason == "missing":
        rows[3]["detections"] = []
    if reason == "clipped":
        rows[3]["detections"][0]["boundary_clipped"] = True
    tracks = track_handles(rows, "sha")["tracks"]
    assert len(tracks) == 2
    assert sorted([p["sample_index"] for p in t["points"]] for t in tracks) == [[0, 1, 2], [4, 5, 6]]


def test_sampling_gaps_color_changes_and_ambiguous_contacts_split_fragments():
    gap = [sample(i) for i in (0, 1, 2, 6, 7, 8)]
    assert len(track_handles(gap, "sha")["tracks"]) == 2
    changed = [sample(i, colors=["red" if i < 3 else "yellow"]) for i in range(6)]
    assert len(track_handles(changed, "sha")["tracks"]) == 2
    positions = [(20, 120), (35, 105), (50, 90), (68, 72), (85, 55), (100, 40), (115, 25)]
    result = track_handles([sample(i, p) for i, p in enumerate(positions)], "sha")
    assert len(result["tracks"]) == 4
    assert result["diagnostics"]["ambiguous_candidates_excluded"] == 2
    assert all(not any(p["sample_index"] == 3 for p in t["points"]) for t in result["tracks"])
    assert all(t["start_seconds"] >= 2 or t["end_seconds"] <= 1 for t in result["tracks"])


def test_nearby_same_color_candidates_and_short_runs_cannot_create_tracks():
    assert track_handles([sample(i, (20, 22)) for i in range(5)], "sha")["tracks"] == []
    assert track_handles([sample(0), sample(1)], "sha")["tracks"] == []
    rows = [sample(i, requested_interval_seconds=0.25, source_seconds=i * 0.25) for i in range(3)]
    assert track_handles(rows, "sha")["tracks"] == []


@pytest.mark.usefixtures("fake_window")
def test_new_windows_derive_automatically_and_cached_derivation_never_fetches_video(db, sources, monkeypatch):
    windows.enqueue_window(
        db, windows.WindowRequest(source_id=sources[0]["id"], start_seconds=100, end_seconds=105)
    )
    assert worker.run_one(db)["state"] == "succeeded"
    window = catalog(db)["analysis_windows"][0]
    with db.session() as s:
        existing = s.scalar(select(WindowTracks))
        assert existing is not None
        expected_id = existing.id
    monkeypatch.setattr(
        windows, "resolve_media", lambda *_: pytest.fail("Cached tracks must not fetch media")
    )
    result = derive_window_tracks(db, window["id"])
    assert result["id"] == expected_id and len(result["tracks"]) == 2
    assert result["input_parquet"] == window["parquet"]
    assert result["context_anchor_evidence_ids"] == window["anchor_evidence_ids"]
    client = TestClient(create_app(db.settings))
    assert client.post(f"/api/windows/{window['id']}/tracks").json()["sha256"] == result["sha256"]
    assert client.get(f"/api/window-tracks/{expected_id}").json()["tracks"] == result["tracks"]
    assert client.get("/api/catalog").json()["window_tracks"][0]["track_count"] == 2
    release = snapshot(db, "exploratory")
    assert release["window_tracks"][0]["id"] == result["id"]
    with db.session() as s:
        assert len(list(s.scalars(select(WindowTracks)))) == 1
        row = s.get(AnalysisWindow, window["id"])
        row.document = {**row.document, "source_metadata_sha256": "forged"}
    with pytest.raises(ValueError, match="source window"):
        snapshot(db, "exploratory")
    with pytest.raises(ValueError, match="manifest disagrees"):
        derive_window_tracks(db, window["id"])


@pytest.mark.usefixtures("fake_window")
def test_corrupt_dense_and_row_provenance_are_rejected(db, sources):
    windows.enqueue_window(
        db, windows.WindowRequest(source_id=sources[0]["id"], start_seconds=10, end_seconds=13)
    )
    assert worker.run_one(db)["state"] == "succeeded"
    w = catalog(db)["analysis_windows"][0]
    rows = load_track_input(db.settings.data_dir, w)
    for change in (
        {"source_id": "other"},
        {"model_version": "older"},
        {"visibility_version": "older"},
        {"source_seconds": 99},
        {"decoded_rgb_sha256": "another-image"},
    ):
        bad = copy.deepcopy(rows)
        bad[0].update(change)
        with pytest.raises(ValueError):
            validate_rows(w, bad)
    bad = copy.deepcopy(rows)
    bad[1] = bad[0]
    with pytest.raises(ValueError, match="duplicate"):
        validate_rows(w, bad)
    bad = copy.deepcopy(rows)
    bad[0]["detections"][0]["center_px"] = [float("nan"), 0]
    with pytest.raises(ValueError, match="coordinates"):
        validate_rows(w, bad)
    (db.settings.data_dir / w["parquet"]["path"]).write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="checksum"):
        derive_window_tracks(db, w["id"])


@pytest.mark.usefixtures("fake_window")
def test_track_result_limit_preserves_acquired_observations(db, sources, monkeypatch):
    from hogline.analytics import tracks

    monkeypatch.setattr(tracks, "MAX_RESULT_BYTES", 1)
    windows.enqueue_window(
        db, windows.WindowRequest(source_id=sources[0]["id"], start_seconds=20, end_seconds=23)
    )
    assert worker.run_one(db)["state"] == "succeeded"
    w = catalog(db)["analysis_windows"][0]
    assert load_track_input(db.settings.data_dir, w)
    result = derive_window_tracks(db, w["id"])
    assert result["status"] == "result_limit_exceeded"
    assert result["tracks"] == []
    assert result["diagnostics"]["fragments_excluded_by_result_limit"] == 2
    assert snapshot(db, "exploratory")["window_tracks"][0]["status"] == "result_limit_exceeded"
