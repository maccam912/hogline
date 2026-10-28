import copy

import numpy as np
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from hogline.adapters.sources import digest
from hogline.analytics.actors import derive_window_actors, load_actor_input, validate_actor_rows
from hogline.analytics.exports import snapshot
from hogline.db import AnalysisWindow, WindowActors
from hogline.domain.catalog import catalog
from hogline.orchestration import windows, worker
from hogline.perception.actors import actor_fragments
from hogline.perception.people import PersonDetector, decode_people, optional_person_detector
from hogline.review.app import create_app
from tests.test_dense_analysis import fake_window  # noqa: F401


def descriptor(n=0):
    return [1.0 if i == n else 0.0 for i in range(32)]


def person(x=50, color=0, **kw):
    return {
        "bbox_px": [x, 100, 40, 80],
        "raw_model_score": 0.8,
        "score_kind": "uncalibrated",
        "boundary_clipped": False,
        "torso_hsv_histogram": descriptor(color),
        **kw,
    }


def sample(i, people=None, **kw):
    return {
        "sample_index": i,
        "source_seconds": i * 0.5,
        "pts": i,
        "time_base_num": 1,
        "time_base_den": 2,
        "decoded_rgb_sha256": f"image-{i}",
        "youtube_url": None,
        "view": "perspective_left",
        "crop_width": 300,
        "crop_height": 500,
        "requested_interval_seconds": 0.5,
        "people": people if people is not None else [person(50 + i * 5)],
        **kw,
    }


def predictions(boxes, ratio=1):
    output = np.zeros((1, 8400, 85), dtype=np.float32)
    for i, (x, y, w, h) in enumerate(boxes):
        output[0, i, :4] = [
            (x + w / 2) * ratio / 8 - i,
            (y + h / 2) * ratio / 8,
            np.log(w * ratio / 8),
            np.log(h * ratio / 8),
        ]
        output[0, i, 4:6] = 0.9
    return output


def test_pinned_person_postprocessing_restores_non_square_crop_and_does_not_normalize_rgb():
    rgb = np.full((400, 200, 3), [230, 20, 20], dtype=np.uint8)
    output = predictions([(50, 100, 40, 80), (51, 101, 40, 80)], 1.6)
    detections = decode_people(output, rgb, 1.6)
    assert len(detections) == 1  # NMS suppresses duplicate boxes.
    assert detections[0]["bbox_px"] == pytest.approx([50, 100, 40, 80], abs=0.001)
    assert sum(detections[0]["torso_hsv_histogram"]) == pytest.approx(1)
    assert not detections[0]["boundary_clipped"]
    assert decode_people(predictions([(300, 100, 20, 40)], 1.6), rgb, 1.6) == []  # Letterbox padding.
    clipped = decode_people(predictions([(-10, 100, 40, 80)], 1.6), rgb, 1.6)[0]
    assert clipped["boundary_clipped"] and clipped["bbox_px"][0] == 0

    class Network:
        def setInput(self, blob):
            self.blob = blob

        def forward(self):
            return output

    detector = PersonDetector.__new__(PersonDetector)
    detector.net = Network()
    detector.detect(rgb)
    assert detector.net.blob.shape == (1, 3, 640, 640)
    assert detector.net.blob[0, :, 0, 0].tolist() == [230, 20, 20]
    assert detector.net.blob[0, :, 0, 639].tolist() == [114, 114, 114]


def test_missing_invalid_and_oversized_person_models_abstain_without_network_access(tmp_path, monkeypatch):
    monkeypatch.delenv("HOGLINE_PERSON_MODEL", raising=False)
    detector, status = optional_person_detector(tmp_path)
    assert detector is None and status["missing_reason"] == "pinned_person_model_not_installed"
    fake = tmp_path / "model.onnx"
    fake.write_bytes(b"wrong model")
    monkeypatch.setenv("HOGLINE_PERSON_MODEL", str(fake))
    assert (
        optional_person_detector(tmp_path)[1]["missing_reason"] == "pinned_person_model_invalid_or_unloadable"
    )
    with pytest.raises(ValueError, match="contract"):
        decode_people(np.zeros((1, 1, 85)), np.zeros((640, 640, 3), dtype=np.uint8), 1)
    from hogline.perception import people

    monkeypatch.setattr(people, "MAX_PEOPLE", 1)
    with pytest.raises(ValueError, match="count"):
        decode_people(
            predictions([(50, 100, 30, 60), (150, 100, 30, 60)]), np.zeros((640, 640, 3), dtype=np.uint8), 1
        )


def test_actor_fragments_keep_anonymous_identity_separate_from_deliveries():
    rows = [sample(i, [person(40 + i * 5), person(200 - i * 3, 1)]) for i in range(8)]
    result = actor_fragments(rows, "input")
    assert result == actor_fragments(list(reversed(rows)), "input")
    assert len(result["tracks"]) == 2
    assert all(t["person_id"] is t["game_actor_id"] is t["delivery_id"] is None for t in result["tracks"])
    assert all(t["sheet_membership"] == "unresolved" for t in result["tracks"])
    assert all([p["pts"] for p in t["points"]] == list(range(8)) for t in result["tracks"])
    assert result["tracks"][0]["id"] != actor_fragments(rows, "other")["tracks"][0]["id"]


@pytest.mark.parametrize("problem", ["clipped", "missing", "appearance", "gap"])
def test_occlusion_clipping_clothing_changes_and_gaps_split_actors(problem):
    rows = [sample(i) for i in range(8)]
    if problem == "clipped":
        rows[4]["people"][0]["boundary_clipped"] = True
    if problem == "missing":
        rows[4]["people"] = []
    if problem == "appearance":
        for r in rows[4:]:
            r["people"][0]["torso_hsv_histogram"] = descriptor(1)
    if problem == "gap":
        for r in rows[4:]:
            r["sample_index"] += 1
            r["source_seconds"] += 2
    tracks = actor_fragments(rows, "input")["tracks"]
    assert len(tracks) == 2
    assert all(t["end_seconds"] <= 1.5 or t["start_seconds"] >= 2 for t in tracks)


def test_same_clothing_ambiguous_bodies_do_not_create_identity_through_contacts():
    rows = [sample(i, [person(50), person(60)]) for i in range(6)]
    result = actor_fragments(rows, "input")
    assert result["tracks"] == []
    assert result["diagnostics"]["ambiguous_people_excluded"] > 0


@pytest.fixture
def fake_people(monkeypatch):
    class Detector:
        calls = 0

        def detect(self, rgb):
            self.calls += 1
            return [person(40 + self.calls // 2)]

    detector = Detector()
    monkeypatch.setattr(
        windows,
        "optional_person_detector",
        lambda *_: (
            detector,
            {
                "status": "available",
                "model_sha256": windows.MODEL_SHA256,
            },
        ),
    )
    return detector


@pytest.mark.usefixtures("fake_window", "fake_people")
def test_automatic_actor_pipeline_cached_api_and_release_lineage(db, sources, monkeypatch):
    windows.enqueue_window(
        db, windows.WindowRequest(source_id=sources[0]["id"], start_seconds=100, end_seconds=105)
    )
    assert worker.run_one(db)["state"] == "succeeded"
    (window,) = catalog(db)["analysis_windows"]
    rows = load_actor_input(db.settings.data_dir, window)
    assert len(rows) == 20 and rows[0]["pts"] == 100000
    monkeypatch.setattr(
        windows, "resolve_media", lambda *_: pytest.fail("Cached actors must not fetch video")
    )
    result = derive_window_actors(db, window["id"])
    assert len(result["tracks"]) == 2
    assert result["tracks"][0]["points"][0]["source_seconds"] == 100
    client = TestClient(create_app(db.settings))
    assert client.post(f"/api/windows/{window['id']}/actors").json() == result
    assert client.get(f"/api/window-actors/{result['id']}").json() == result
    assert client.get("/api/catalog").json()["window_actors"][0]["track_count"] == 2
    assert snapshot(db, "exploratory")["window_actors"][0] == result
    with db.session() as s:
        assert len(list(s.scalars(select(WindowActors)))) == 1
        row = s.get(AnalysisWindow, window["id"])
        row.document = {**row.document, "profile_sha256": "changed"}
    with pytest.raises(ValueError, match="source window"):
        snapshot(db, "exploratory")
    with pytest.raises(ValueError, match="manifest disagrees"):
        derive_window_actors(db, window["id"])


@pytest.mark.usefixtures("fake_window", "fake_people")
def test_actor_input_corruption_and_immutable_result_limit(db, sources, monkeypatch):
    from hogline.analytics import actors

    monkeypatch.setattr(actors, "MAX_RESULT_BYTES", 1)
    windows.enqueue_window(
        db, windows.WindowRequest(source_id=sources[0]["id"], start_seconds=100, end_seconds=105)
    )
    assert worker.run_one(db)["state"] == "succeeded"
    (window,) = catalog(db)["analysis_windows"]
    assert derive_window_actors(db, window["id"])["status"] == "result_limit_exceeded"
    rows = load_actor_input(db.settings.data_dir, window)
    for change in ({"source_id": "other"}, {"source_seconds": 0}, {"pts": 50}):
        bad = copy.deepcopy(rows)
        bad[0].update(change)
        with pytest.raises(ValueError):
            validate_actor_rows(window, bad)
    bad = copy.deepcopy(rows)
    bad[0]["people"][0]["bbox_px"][0] = float("nan")
    with pytest.raises(ValueError, match="geometry"):
        validate_actor_rows(window, bad)
    (db.settings.data_dir / window["actor_input"]["parquet"]["path"]).write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="checksum"):
        derive_window_actors(db, window["id"])


@pytest.mark.usefixtures("fake_window")
def test_missing_or_failed_optional_inference_preserves_overhead_window(db, sources, monkeypatch):
    class Broken:
        def detect(self, rgb):
            raise ValueError("bad inference")

    monkeypatch.setattr(windows, "optional_person_detector", lambda *_: (Broken(), {"status": "available"}))
    windows.enqueue_window(
        db, windows.WindowRequest(source_id=sources[0]["id"], start_seconds=100, end_seconds=105)
    )
    assert worker.run_one(db)["state"] == "succeeded"
    (window,) = catalog(db)["analysis_windows"]
    assert window["actor_input"]["missing_reason"] == "person_inference_failed"
    assert snapshot(db, "exploratory")["window_actors"][0]["status"] == "unavailable"
    with pytest.raises(ValueError, match="unavailable"):
        derive_window_actors(db, window["id"])
    with db.session() as s:
        row = s.scalar(select(WindowActors))
        row.document = {**row.document, "notice": "corrupt"}
        assert digest(row.document) != row.sha256
    assert TestClient(create_app(db.settings)).get("/api/catalog").status_code == 422


@pytest.mark.usefixtures("fake_window")
def test_person_inference_cannot_swallow_batch_cancellation(db, sources, monkeypatch):
    clock, offset = windows.time.monotonic, [0]
    monkeypatch.setattr(windows.time, "monotonic", lambda: clock() + offset[0])

    class Detector:
        def detect(self, rgb):
            offset[0] += 2  # The next perspective-view tick must recheck the owning execution.
            return [person()]

    def check(*args, **kwargs):
        if offset[0]:
            raise ValueError("batch cancelled during person analysis")

    monkeypatch.setattr(windows, "optional_person_detector", lambda *_: (Detector(), {"status": "available"}))
    monkeypatch.setattr(windows, "check_execution", check)
    windows.enqueue_window(
        db,
        windows.WindowRequest(
            source_id=sources[0]["id"],
            start_seconds=100,
            end_seconds=105,
        ),
    )
    assert worker.run_one(db)["state"] == "failed"
    assert catalog(db)["analysis_windows"] == []
