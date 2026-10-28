import copy

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from hogline.adapters.sources import digest
from hogline.analytics.exports import snapshot
from hogline.analytics.house_states import derive_house_states
from hogline.db import AnalysisWindow, WindowHouseStates
from hogline.domain.catalog import catalog
from hogline.orchestration import windows, worker
from hogline.reconstruction.house_states import house_states
from hogline.review.app import create_app
from tests.test_dense_analysis import fake_window  # noqa: F401
from tests.test_tracking import sample


def row(i, positions=(30,), **kw):
    return sample(
        i,
        positions,
        motion_mean_abs=0,
        rejected_candidates=[],
        ring_reference={
            "status": "candidate",
            "center_px": [100, 100],
            "axes_px": [160, 120],
            "angle_degrees": 90,
        },
        **kw,
    )


def test_states_and_clearing_brackets_preserve_source_times_and_null_end_claims():
    rows = [row(i, (30,) if i < 4 else ()) for i in range(8)]
    result = house_states(rows, "input")
    assert result == house_states(list(reversed(rows)), "input")
    assert [s["kind"] for s in result["states"]] == [
        "stable_handle_configuration",
        "stable_no_handle_detections",
    ]
    (change,) = result["transitions"]
    assert change["kind"] == "possible_clearing"
    assert (change["earliest_seconds"], change["latest_seconds"]) == (1.5, 2)
    assert change["before"]["pts"] == 3 and change["after"]["pts"] == 4
    assert change["after"]["youtube_url"].endswith("&t=2s")
    assert change["played_end_boundary"] is change["official_end_number"] is None
    assert all(s["score"] is None and s["acceptance"] == "proposed" for s in result["states"])
    assert result["states"][0]["id"] != house_states(rows, "other-input")["states"][0]["id"]
    assert house_states([row(i, ()) for i in range(8)], "input")["transitions"] == []
    appear = house_states([row(i, () if i < 4 else (30,)) for i in range(8)], "input")
    assert appear["transitions"][0]["kind"] == "handles_appear"


@pytest.mark.parametrize("problem", ["foreground", "rejected", "clipped", "ring", "motion", "duplicate"])
def test_visibility_and_duplicate_images_abstain(problem):
    rows = [row(i) for i in range(8)]
    for r in rows:
        if problem == "foreground":
            r["foreground_regions"] = [{"kind": "obstruction"}]
        elif problem == "rejected":
            r["rejected_candidates"] = [{"rejection_reasons": ["too_small"]}]
        elif problem == "clipped":
            r["detections"][0]["boundary_clipped"] = True
        elif problem == "ring":
            r["ring_reference"]["center_px"][0] = float("nan")
        elif problem == "motion":
            r["motion_mean_abs"] = 3
        else:
            r["decoded_rgb_sha256"] = "repeated"
    assert house_states(rows, "input")["states"] == []


@pytest.mark.parametrize("problem", ["gap", "geometry", "missing_ring", "long", "crop"])
def test_uncertain_camera_or_sample_gaps_cannot_become_clearing_events(problem):
    rows = [row(i, (30,) if i < 4 else ()) for i in range(8)]
    if problem == "gap":
        for r in rows[4:]:
            r["sample_index"] += 1
    elif problem == "long":
        for r in rows[4:]:
            r["source_seconds"] += 12
    elif problem == "geometry":
        for r in rows[4:]:
            r["ring_reference"]["center_px"][0] += 10
    elif problem == "crop":
        for r in rows[4:]:
            r["crop_width"] += 10
    else:
        # A single unobservable frame remains unknown between the states.
        rows.insert(4, row(4, ()))
        for i, r in enumerate(rows):
            r["sample_index"], r["source_seconds"] = i, i * 0.5
        rows[4]["ring_reference"] = {"status": "unavailable"}
    result = house_states(rows, "input")
    assert len(result["states"]) == 2
    assert not result["transitions"]
    assert result["diagnostics"]["unresolved_change_gaps"] == 1


def test_stability_does_not_follow_slow_handle_or_camera_drift():
    assert not house_states([row(i, (30 + i,)) for i in range(4)], "input")["states"]
    rows = [row(i) for i in range(4)]
    for i, r in enumerate(rows):
        r["ring_reference"]["center_px"][0] += i
    assert not house_states(rows, "input")["states"]
    assert not house_states([row(i) for i in range(3)], "input")["states"]
    changed = house_states([row(i, (30,) if i < 4 else (80,)) for i in range(8)], "input")
    assert changed["transitions"][0]["kind"] == "configuration_changed"


@pytest.mark.usefixtures("fake_window")
def test_cached_derivation_api_export_idempotency_and_integrity(db, sources, monkeypatch):
    windows.enqueue_window(
        db, windows.WindowRequest(source_id=sources[0]["id"], start_seconds=100, end_seconds=105)
    )
    assert worker.run_one(db)["state"] == "succeeded"
    (window,) = catalog(db)["analysis_windows"]
    monkeypatch.setattr(
        windows, "resolve_media", lambda *_: pytest.fail("Cached states must not fetch video")
    )
    result = derive_house_states(db, window["id"])
    assert result["status"] == "derived" and result["states"] == []
    assert result["input_parquet"] == window["parquet"]
    client = TestClient(create_app(db.settings))
    assert client.post(f"/api/windows/{window['id']}/house-states").json() == result
    assert client.get(f"/api/window-house-states/{result['id']}").json() == result
    assert client.get("/api/catalog").json()["window_house_states"][0]["state_count"] == 0
    assert snapshot(db, "exploratory")["window_house_states"][0] == result
    with db.session() as session:
        assert len(list(session.scalars(select(WindowHouseStates)))) == 1
        stored = session.get(AnalysisWindow, window["id"])
        stored.document = {**stored.document, "profile_sha256": "changed"}
    with pytest.raises(ValueError, match="source window"):
        snapshot(db, "exploratory")
    with pytest.raises(ValueError, match="manifest disagrees"):
        derive_house_states(db, window["id"])


@pytest.mark.usefixtures("fake_window")
def test_house_result_checksum_and_output_size_abstention(db, sources, monkeypatch):
    from hogline.analytics import house_states as module

    monkeypatch.setattr(module, "MAX_RESULT_BYTES", 1)
    windows.enqueue_window(
        db,
        windows.WindowRequest(
            source_id=sources[0]["id"],
            start_seconds=20,
            end_seconds=23,
        ),
    )
    assert worker.run_one(db)["state"] == "succeeded"
    (window,) = catalog(db)["analysis_windows"]
    result = derive_house_states(db, window["id"])
    assert result["status"] == "result_limit_exceeded" and not result["states"]
    with db.session() as session:
        stored = session.get(WindowHouseStates, result["id"])
        doc = copy.deepcopy(stored.document)
        doc["notice"] = "corrupt"
        assert digest(doc) != stored.sha256
        stored.document = doc
    assert (
        TestClient(create_app(db.settings)).get(f"/api/window-house-states/{result['id']}").status_code == 422
    )
    with pytest.raises(ValueError, match="checksum"):
        snapshot(db, "exploratory")


@pytest.mark.usefixtures("fake_window")
def test_nonempty_states_keep_release_timing_references(db, sources, monkeypatch):
    decoder, inspector = windows.decode_window, windows.HSVHandleDetector.inspect

    def distinct_frames(*args, **kwargs):
        for i, (image, timing) in enumerate(decoder(*args, **kwargs)):
            image.putpixel((0, 0), (i, i, i))
            yield image, timing

    def without_fixture_speck(self, image):
        result = inspector(self, image)
        return {**result, "rejected_candidates": []}

    monkeypatch.setattr(windows, "decode_window", distinct_frames)
    monkeypatch.setattr(windows.HSVHandleDetector, "inspect", without_fixture_speck)
    windows.enqueue_window(
        db,
        windows.WindowRequest(
            source_id=sources[0]["id"],
            start_seconds=100,
            end_seconds=105,
        ),
    )
    assert worker.run_one(db)["state"] == "succeeded"
    release = snapshot(db, "exploratory")
    (state,) = release["window_house_states"][0]["states"]
    assert state["candidate_count"] == 2 and state["color_counts"] == {"red": 1, "yellow": 1}
    assert state["start_seconds"] == 100 and state["end_seconds"] == 104.5
    with db.session() as session:
        row = session.scalar(select(WindowHouseStates))
        document = copy.deepcopy(row.document)
        document["states"][0]["first"]["source_seconds"] = 101
        row.document, row.sha256 = document, digest(document)
    with pytest.raises(ValueError, match="house-state timing"):
        snapshot(db, "exploratory")
