import copy

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from hogline.analytics.end_timeline import analyze_house_scores, house_scores_in_session
from hogline.analytics.exports import snapshot
from hogline.db import Evidence, FrameObservation
from hogline.orchestration import windows, worker
from hogline.perception.house_score import granite_footprint, score_footprints
from hogline.review.app import create_app
from tests.test_dense_analysis import fake_window  # noqa: F401

RING = {"status": "candidate", "center_px": [200, 200], "axes_px": [320, 320], "angle_degrees": 0}


def stone(color, x, y=200, radius=14):
    return {
        "status": "candidate",
        "color": color,
        "center_px": [x, y],
        "radius_px": radius,
        "boundary_error_px": 1,
    }


def test_scores_use_granite_edges_and_exclude_outside_stones():
    result = score_footprints(
        [stone("red", 220), stone("red", 260), stone("yellow", 300), stone("yellow", 390)], RING
    )
    assert result["provisional_score"] == {"red": 2, "yellow": 0}
    assert result["measurements"][-1]["eligibility"] == "outside"
    # A farther center can still have the nearest granite edge when its footprint is larger.
    result = score_footprints(
        [stone("red", 250, radius=10), stone("yellow", 260, radius=35)], RING, uncertainty=0
    )
    assert result["provisional_score"] == {"red": 0, "yellow": 1}
    assert score_footprints([stone("red", 200)], RING)["provisional_score"] == {"red": 1, "yellow": 0}


def test_close_colors_biters_unknown_boundaries_and_missing_handles_abstain():
    assert score_footprints([stone("red", 250), stone("yellow", 251)], RING)["provisional_score"] is None
    assert score_footprints([stone("red", 373)], RING)["provisional_score"] is None
    assert score_footprints([], RING)["provisional_score"] is None
    assert score_footprints([stone("red", 220), {"status": "unresolved"}], RING)["provisional_score"] is None
    assert score_footprints([stone("red", 220)], {"status": "unavailable"})["provisional_score"] is None
    score = score_footprints([stone("red", 220), stone("red", 280), stone("yellow", 281)], RING)
    assert score["provisional_score"] is None and score["reasons"] == ["point_count_requires_measurement"]
    with pytest.raises(ValueError):
        score_footprints([stone("red", float("nan"))], RING)
    with pytest.raises(ValueError):
        score_footprints([stone("red", 200)], {**RING, "axes_px": [0, 20]})


def test_circular_edge_estimator_recovers_stone_body_and_rejects_handle_only():
    image = np.full((400, 400, 3), 230, dtype=np.uint8)
    cv2.circle(image, (200, 200), 20, (75, 75, 75), -1)
    cv2.circle(image, (200, 200), 9, (230, 20, 20), -1)
    handle = {
        "center_px": [200, 200],
        "color": "red",
        "boundary_clipped": False,
        "bbox_px": [191, 191, 18, 18],
    }
    result = granite_footprint(image, handle, 0)
    assert result["status"] == "candidate" and 18 <= result["radius_px"] <= 22
    assert granite_footprint(np.full_like(image, 230), handle, 0)["status"] == "unresolved"
    assert granite_footprint(image, {**handle, "boundary_clipped": True}, 0)["status"] == "unresolved"


@pytest.mark.usefixtures("fake_window")
def test_score_workflow_pins_inputs_and_exports_without_video(db, sources, monkeypatch):
    windows.enqueue_window(
        db, windows.WindowRequest(source_id=sources[0]["id"], start_seconds=20, end_seconds=23)
    )
    assert worker.run_one(db)["state"] == "succeeded"
    monkeypatch.setattr(windows, "resolve_media", lambda *_: pytest.fail("Must use cached crops"))
    result = analyze_house_scores(db, sources[0]["id"], limit=1)
    assert result["processed_frames"] == 1
    (observation,) = result["observations"]
    assert observation["decoded_rgb_sha256"] and observation["input_crops"]
    assert all(v["official_end_number"] is None for v in observation["views"].values())
    client = TestClient(create_app(db.settings))
    assert client.get(f"/api/house-scores/{observation['id']}").json() == observation
    assert client.post(f"/api/sources/{sources[0]['id']}/house-scores", json={"limit": 24}).status_code == 200
    assert snapshot(db, "exploratory")["house_score_observations"]
    with db.session() as session:
        stored = session.get(FrameObservation, observation["id"])
        altered = copy.deepcopy(stored.document)
        altered["source_seconds"] += 1
        stored.document = altered
    with db.session() as session, pytest.raises(ValueError, match="checksum"):
        house_scores_in_session(session)


@pytest.mark.usefixtures("fake_window")
def test_corrupt_originals_fail_and_work_is_bounded(db, sources):
    windows.enqueue_window(
        db, windows.WindowRequest(source_id=sources[0]["id"], start_seconds=20, end_seconds=23)
    )
    assert worker.run_one(db)["state"] == "succeeded"
    with db.session() as session:
        frame = session.scalar(select(Evidence))
        path = db.settings.data_dir / frame.document["image"]["path"]
        path.write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="original frame"):
        analyze_house_scores(db, sources[0]["id"])
    for kwargs in ({"limit": 121}, {"max_seconds": float("nan")}, {"max_seconds": 121}):
        with pytest.raises(ValueError):
            analyze_house_scores(db, sources[0]["id"], **kwargs)
