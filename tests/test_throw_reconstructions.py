import copy
import json
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from hogline.db import Job
from hogline.orchestration import throw_reconstructions as jobs
from hogline.orchestration.worker import claim_transaction
from hogline.perception.delivery_ray import fit_ray, image_points
from hogline.perception.throw_reconstruction import associate, orientation, smooth, tee_time, video_maps
from hogline.review.app import create_app


def ray_fixture(sign=-1):
    geometry = {
        "projection": {
            "pixel_normalized_to_house": np.eye(3).tolist(),
            "origin_px": [160, 160],
            "scale_px": 140,
        },
        "rotation": np.eye(2).tolist(),
    }
    s = np.linspace(-2, 2, 101)
    pixels = image_points(np.column_stack((0.1 + 0.02 * s, sign * s)), geometry)
    points = [{"pixel": p.tolist(), "pts": 900000 + i * 1500, "t": 10 + i / 60} for i, p in enumerate(pixels)]
    ray = fit_ray(points, geometry, sign)
    ray.update(
        id="native-ray",
        origin_view="house_bottom" if sign == -1 else "house_top",
        receiving_view="house_top" if sign == -1 else "house_bottom",
        geometry=geometry,
        receiving_geometry=geometry,
        perspectives={"perspective_left": {}},
        media_sha256="media",
    )
    return ray


@pytest.fixture
def example_report(db, sources, monkeypatch):
    source = sources[0]
    ray = ray_fixture()
    ray.update(source_id=source["id"], source_metadata_sha256=source["metadata_sha256"])
    report = {
        "game_id": "game",
        "label": "Team",
        "stale": False,
        "review_sha256": "review",
        "players": [
            {
                "person_id": "p",
                "name": "Anonymous P1",
                "position": "lead",
                "deliveries": [
                    {
                        "candidate_id": "candidate",
                        "seconds": 10,
                        "end": 1,
                        "finish": "unknown",
                        "delivery_ray": ray,
                    }
                ],
            }
        ],
    }
    monkeypatch.setattr(jobs, "estimate_report", lambda db, g: copy.deepcopy(report))
    monkeypatch.setattr(jobs, "latest_revision", lambda s, g: SimpleNamespace(sha256=report["review_sha256"]))
    return report


@pytest.mark.parametrize("sign", [-1, 1])
def test_alignment_uses_observed_tee_and_opposite_direction_rotates_x(sign):
    ray = ray_fixture(sign)
    assert tee_time(ray) == pytest.approx(10 + 50 / 60)
    assert orientation(ray) == (1 if sign == -1 else -1)
    maps = video_maps(ray, {})
    h = np.array(next(m["matrix"] for m in maps if m["view"] == ray["origin_view"]))
    p = h @ np.array([orientation(ray) * 0.3, 1, 1])
    expected = image_points([[0.3, sign]], ray["geometry"])[0]
    assert p[:2] / p[2] == pytest.approx(expected, abs=1e-4)


def test_no_tee_crossing_does_not_invent_release_alignment():
    ray = ray_fixture()
    ray["points"] = [p for p in ray["points"] if p["ice_m"][1] > 0]
    with pytest.raises(ValueError, match="No observed"):
        tee_time(ray)


def test_existing_stationary_yellow_cannot_start_receiving_track():
    ray = ray_fixture()
    last = ray["points"][-1]["t"]
    stationary = {
        "pixel": [150, 150],
        "view": ray["receiving_view"],
        "world_m": [0.3, 4],
        "kind": "overhead_handle",
    }
    rows = [
        {"t": 9, "detections": [stationary]},
        {"t": last + 0.75, "detections": [stationary], "timing": {}, "frame_sha256": "x"},
    ]
    result = associate(rows, ray)
    assert all(p["view"] == ray["origin_view"] for p in result)


def test_partial_smoother_ends_at_last_observation_instead_of_assumed_stop():
    ray = ray_fixture()
    observations = [
        {**p, "view": ray["origin_view"], "world_m": [p["ice_m"][0], -p["ice_m"][1]]} for p in ray["points"]
    ]
    result = smooth(observations, ray)
    assert result["status"] == "partial_track"
    assert result["grid"][-1][0] == pytest.approx(ray["last_seconds"] - tee_time(ray))
    assert max(p[2] for p in result["grid"]) < 3
    assert np.diff(np.array(result["grid"])[:, 2]).min() >= -1e-8
    assert smooth(observations, ray, contact=True)["status"] == "contact_uncertain"


def test_queue_is_explicit_deduplicated_and_rejects_foreign_candidates(db, example_report):
    assert jobs.catalog(db, "game")["throws"][0]["state"] == "not_generated"
    with db.snapshot_session() as s:
        assert not list(s.scalars(select(Job)))
    request = jobs.ReconstructionRequest(candidate_ids=["candidate", "candidate"])
    a = jobs.enqueue_reconstructions(db, "game", request)
    b = jobs.enqueue_reconstructions(db, "game", request)
    assert len(a["throws"]) == 1 and a == b
    with pytest.raises(ValueError, match="belongs"):
        jobs.enqueue_reconstructions(db, "game", jobs.ReconstructionRequest(candidate_ids=["other"]))
    example_report["stale"] = True
    with pytest.raises(ValueError, match="Refresh"):
        jobs.enqueue_reconstructions(db, "game", request)


def test_reconstruction_jobs_are_serialized_without_blocking_other_work(db, example_report):
    jobs.enqueue_reconstructions(db, "game", jobs.ReconstructionRequest())
    first = claim_transaction(db)
    assert first["kind"] == "throw_reconstruction"
    jobs.enqueue_reconstructions(db, "game", jobs.ReconstructionRequest(include_clip=True))
    assert claim_transaction(db) is None
    with db.snapshot_session(write=True) as s:
        s.add(Job(id="other", job_key="other", document={"request": {"source_id": "source"}, "kind": "scan"}))
    assert claim_transaction(db)["id"] == "other"


def test_worker_publishes_checked_files_and_corrupt_output_is_withheld(db, example_report, monkeypatch):
    from PIL import Image

    from hogline.domain import delivery_rays

    jobs.enqueue_reconstructions(db, "game", jobs.ReconstructionRequest())
    job = claim_transaction(db)
    ray = example_report["players"][0]["deliveries"][0]["delivery_ray"]
    monkeypatch.setattr(jobs, "resolve_media", lambda s: {"sha256": "media", "uri": "unused"})
    monkeypatch.setattr(jobs, "register_common_sheet", lambda r: {})
    monkeypatch.setattr(jobs, "video_maps", lambda r, c: [])
    monkeypatch.setattr(delivery_rays, "current_rays", lambda *a: {"candidate": ray})
    monkeypatch.setattr(
        jobs,
        "decode_window",
        lambda *a, **k: iter(
            [
                (
                    Image.new("RGB", (1280, 720), "white"),
                    {"pts": 1200000, "time_base_num": 1, "time_base_den": 90000},
                )
            ]
        ),
    )
    monkeypatch.setattr(jobs, "frame_candidates", lambda *a: [])
    result = jobs.execute_reconstruction(db, job)
    current = jobs.catalog(db, "game")["throws"][0]
    assert current["state"] == "ready" and result["candidate_id"] == "candidate"
    from hogline.review import reconstructions

    monkeypatch.setattr(reconstructions, "estimate_report", lambda *a: copy.deepcopy(example_report))
    client = TestClient(create_app(db.settings, start_worker=False))
    base = f"/api/games/game/reconstructions/{current['key']}"
    assert client.get(base + "/manifest.json").status_code == 200
    assert client.get(base + "/trajectory.json").status_code == 200
    assert client.get(base + "/private.txt").status_code == 404
    root = jobs.folder(db, "game", current["key"])
    # A stored result needs its successful job receipt, not just a self-signed manifest.
    saved = json.loads((root / "manifest.json").read_text())
    saved["player"] = "changed"
    jobs.atomic_json(root / "manifest.json", saved)
    assert jobs.checked_result(db, example_report, example_report["players"][0]["deliveries"][0]) is None
    assert jobs.catalog(db, "game")["throws"][0]["state"] == "artifact_unavailable"
    repaired = jobs.enqueue_reconstructions(db, "game", jobs.ReconstructionRequest())
    assert repaired["throws"][0]["state"] == "queued"


def test_viewer_is_read_only_and_exposes_generation_and_per_player_controls(db, example_report, monkeypatch):
    from hogline.review import reconstructions

    monkeypatch.setattr(reconstructions, "estimate_report", lambda *a: example_report)
    client = TestClient(create_app(db.settings, start_worker=False))
    page = client.get("/games/game/reconstructions")
    assert page.status_code == 200 and "Generate all throw paths" in page.text
    assert "delivery-tee crossing" in page.text and 'id="players"' in page.text
    assert client.get("/api/games/game/reconstructions").json()["throws"][0]["state"] == "not_generated"
    assert client.get("/api/games/game/reconstructions/" + "a" * 64 + "/private.txt").status_code == 404
    assert (
        client.post("/api/games/game/reconstructions", json={"candidate_ids": ["candidate"]}).status_code
        == 200
    )
