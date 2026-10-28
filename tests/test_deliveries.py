import copy

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from hogline.analytics.deliveries import derive_window_deliveries
from hogline.analytics.exports import snapshot
from hogline.db import WindowDeliveries
from hogline.domain.catalog import catalog
from hogline.orchestration import windows, worker
from hogline.reconstruction.deliveries import release_associations
from hogline.review.app import create_app
from tests.test_actors import fake_people  # noqa: F401
from tests.test_dense_analysis import fake_window  # noqa: F401


def control_fixture():
    rows, points = [], []
    for i in range(11):
        x = 80 if i < 3 else 80 + (i - 2) * 10 if i < 7 else 150 + (i - 7) * 15
        hand = x if i < 7 else 120
        pose = {
            "status": "observed",
            "landmarks": [
                {
                    "index": 15,
                    "center_px": [hand, 300],
                    "inside_crop": True,
                    "raw_visibility": 0.99,
                    "raw_presence": 0.99,
                }
            ],
        }
        rows.append({"view": "perspective_right", "sample_index": i, "people": [{"body_pose": pose}]})
        points.append(
            {
                "sample_index": i,
                "source_seconds": i * 0.25,
                "pts": i,
                "time_base_num": 1,
                "time_base_den": 4,
                "youtube_url": None,
                "center_px": [x, 300],
                "candidate_bbox_px": [x - 6, 294, 13, 13],
                "raw_detection_index": 0,
            }
        )
    actors = [
        {
            "id": "actor-1",
            "view": "perspective_right",
            "points": [
                {
                    "sample_index": i,
                    "person_index": 0,
                }
                for i in range(11)
            ],
        }
    ]
    tracks = [
        {
            "id": "handle-1",
            "view": "perspective_right",
            "color": "red",
            "crop_width": 300,
            "motion_class": "position_change_observed",
            "start_seconds": 0,
            "end_seconds": 2.5,
            "points": points,
        }
    ]
    return rows, actors, tracks


def test_release_requires_stationary_control_coupled_motion_and_observed_separation():
    rows, actors, handles = control_fixture()
    result = release_associations(rows, actors, handles, "input")
    (candidate,) = result["candidates"]
    assert candidate["actor_track_id"] == "actor-1"
    assert (candidate["earliest_release_seconds"], candidate["latest_release_seconds"]) == (1.5, 2)
    assert candidate["before"]["pts"] == 6 and candidate["after"]["pts"] == 8
    assert candidate["control_sample_indices"] == list(range(7))
    assert candidate["separated_sample_indices"] == [8, 9, 10]
    assert candidate["person_id"] is candidate["played_shot_id"] is candidate["weight_measurement"] is None
    assert candidate["acceptance"] == "proposed" and candidate["unresolved"]
    assert result == release_associations(list(reversed(rows)), actors, handles, "input")
    assert candidate["id"] != release_associations(rows, actors, handles, "other")["candidates"][0]["id"]


@pytest.mark.parametrize(
    "problem", ["wrist_hidden", "no_prelude", "no_separation", "actor_gap", "competing_actor"]
)
def test_incomplete_or_ambiguous_delivery_cues_abstain(problem):
    rows, actors, handles = control_fixture()
    if problem == "wrist_hidden":
        for row in rows:
            row["people"][0]["body_pose"]["landmarks"][0]["raw_visibility"] = 0.1
    elif problem == "no_prelude":
        for i, p in enumerate(handles[0]["points"][:3]):
            p["center_px"][0] += i * 10
    elif problem == "no_separation":
        for row, p in zip(rows, handles[0]["points"], strict=True):
            row["people"][0]["body_pose"]["landmarks"][0]["center_px"] = p["center_px"]
    elif problem == "actor_gap":
        actors[0]["points"] = actors[0]["points"][:7]
    else:
        for row in rows:
            row["people"].append(copy.deepcopy(row["people"][0]))
        actors.append(
            {
                "id": "actor-2",
                "view": "perspective_right",
                "points": [{"sample_index": i, "person_index": 1} for i in range(11)],
            }
        )
    result = release_associations(rows, actors, handles, "input")
    assert result["candidates"] == [] and len(result["uncertain_motion"]) == 1
    assert result["diagnostics"]


@pytest.mark.usefixtures("fake_window", "fake_people")
def test_release_pipeline_is_automatic_cached_and_exported(db, sources, monkeypatch):
    windows.enqueue_window(
        db, windows.WindowRequest(source_id=sources[0]["id"], start_seconds=100, end_seconds=105)
    )
    assert worker.run_one(db)["state"] == "succeeded"
    (window,) = catalog(db)["analysis_windows"]
    monkeypatch.setattr(
        windows, "resolve_media", lambda *_: pytest.fail("Cached release analysis must not fetch video")
    )
    result = derive_window_deliveries(db, window["id"])
    assert result["status"] == "derived"
    assert result["input_perspective"] == window["actor_input"]
    client = TestClient(create_app(db.settings))
    assert client.post(f"/api/windows/{window['id']}/deliveries").json() == result
    assert client.get(f"/api/window-deliveries/{result['id']}").json() == result
    assert client.get("/api/catalog").json()["window_deliveries"][0]["candidate_count"] == 0
    assert snapshot(db, "exploratory")["window_deliveries"][0] == result
    with db.session() as s:
        assert len(list(s.scalars(select(WindowDeliveries)))) == 1
        row = s.get(WindowDeliveries, result["id"])
        row.document = {**row.document, "notice": "corrupted"}
    with pytest.raises(ValueError, match="checksum"):
        snapshot(db, "exploratory")


@pytest.mark.usefixtures("fake_window", "fake_people")
def test_release_output_limit_keeps_original_features(db, sources, monkeypatch):
    from hogline.analytics import deliveries

    monkeypatch.setattr(deliveries, "MAX_RESULT_BYTES", 1)
    windows.enqueue_window(
        db, windows.WindowRequest(source_id=sources[0]["id"], start_seconds=100, end_seconds=105)
    )
    assert worker.run_one(db)["state"] == "succeeded"
    (window,) = catalog(db)["analysis_windows"]
    result = derive_window_deliveries(db, window["id"])
    assert result["status"] == "result_limit_exceeded"
    assert result["candidates"] == result["perspective_handle_tracks"] == []
    assert (db.settings.data_dir / window["actor_input"]["parquet"]["path"]).is_file()


@pytest.mark.usefixtures("fake_window", "fake_people")
def test_controlled_release_survives_columnar_pipeline_and_checks_export_references(db, sources, monkeypatch):
    from hogline.adapters.sources import digest

    def positions(i):
        stone = 80 if i < 3 else 80 + (i - 2) * 10 if i < 7 else 150 + (i - 7) * 15
        return stone, stone if i < 7 else 120

    class Pose:
        calls = 0

        def detect(self, rgb, box):
            _, hand = positions(self.calls // 2)
            self.calls += 1
            return {
                "status": "observed",
                "missing_reason": None,
                "raw_model_score": 0.99,
                "landmarks": [
                    {
                        "index": i,
                        "center_px": [hand, 300],
                        "inside_crop": True,
                        "raw_presence": 0.99,
                        "raw_visibility": 0.99,
                    }
                    for i in range(11, 33)
                ],
            }

    pose = Pose()
    counter = [0]

    def handles(rgb, people):
        stone, _ = positions(counter[0] // 2)
        counter[0] += 1
        return {
            "status": "observed",
            "missing_reason": None,
            "detections": [
                {
                    "color": "red",
                    "center_px": [stone, 300],
                    "bbox_px": [stone - 6, 294, 13, 13],
                    "area_px2": 100.0,
                    "shape_score": 0.9,
                    "granite_context_fraction": 0.9,
                    "boundary_clipped": False,
                    "person_overlap": False,
                    "near_wrist": True,
                }
            ],
        }

    monkeypatch.setattr(
        windows,
        "optional_body_pose",
        lambda *_: (
            pose,
            {
                "status": "available",
                "version": windows.POSE_VERSION,
                "model_sha256": windows.POSE_SHA256,
            },
        ),
    )
    monkeypatch.setattr(windows, "perspective_handles", handles)
    windows.enqueue_window(
        db,
        windows.WindowRequest(
            source_id=sources[0]["id"],
            start_seconds=0,
            end_seconds=2.75,
            interval_seconds=0.25,
            max_frames=11,
        ),
    )
    assert worker.run_one(db)["state"] == "succeeded"
    release = snapshot(db, "exploratory")
    (result,) = release["window_deliveries"]
    assert len(result["candidates"]) == 2  # Independent views, never two accepted physical shots.
    assert all(
        (c["earliest_release_seconds"], c["latest_release_seconds"]) == (1.5, 2) for c in result["candidates"]
    )
    assert all(c["played_shot_id"] is None for c in result["candidates"])
    with db.session() as session:
        row = session.get(WindowDeliveries, result["id"])
        document = copy.deepcopy(row.document)
        document["candidates"][0]["before"]["pts"] += 1
        row.document, row.sha256 = document, digest(document)
    with pytest.raises(ValueError, match="delivery timing"):
        snapshot(db, "exploratory")


@pytest.mark.parametrize("fps", [10, 30])
def test_release_control_prelude_uses_elapsed_time_at_high_sample_density(fps):
    rows, actors, tracks = control_fixture()
    rows.clear()
    points = []
    for i in range(int(2.5 * fps) + 1):
        t = i / fps
        x = (
            80
            if t <= 0.5
            else 80 + 40 * (t - 0.5)
            if t <= 1.5
            else 120 + 120 * (t - 1.5)
            if t <= 1.75
            else 150 + 60 * (t - 1.75)
        )
        hand = min(x, 120)
        rows.append(
            {
                "view": "perspective_right",
                "sample_index": i,
                "people": [
                    {
                        "body_pose": {
                            "status": "observed",
                            "landmarks": [
                                {
                                    "index": 15,
                                    "center_px": [hand, 300],
                                    "inside_crop": True,
                                    "raw_visibility": 0.99,
                                    "raw_presence": 0.99,
                                }
                            ],
                        }
                    }
                ],
            }
        )
        points.append(
            {
                "sample_index": i,
                "source_seconds": t,
                "pts": i,
                "time_base_num": 1,
                "time_base_den": fps,
                "youtube_url": None,
                "center_px": [x, 300],
                "candidate_bbox_px": [x - 6, 294, 13, 13],
                "raw_detection_index": 0,
            }
        )
    tracks[0]["points"] = points
    actors[0]["points"] = [{"sample_index": i, "person_index": 0} for i in range(len(rows))]
    result = release_associations(rows, actors, tracks, "dense-input")
    (candidate,) = result["candidates"]
    assert 1.5 <= candidate["earliest_release_seconds"] <= 1.75
    assert 1.75 <= candidate["latest_release_seconds"] <= 2
    assert candidate["latest_release_seconds"] - candidate["earliest_release_seconds"] < 0.5
    # Dense sampling does not make a short moving prelude look stationary.
    for i, p in enumerate(points):
        if p["source_seconds"] <= 0.5:
            p["center_px"][0] += p["source_seconds"] * 20
            rows[i]["people"][0]["body_pose"]["landmarks"][0]["center_px"] = p["center_px"][:]
    bad = release_associations(rows, actors, tracks, "moving-prelude")
    assert not bad["candidates"]
    assert bad["uncertain_motion"][0]["control_intervals"]
