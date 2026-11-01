import copy

import numpy as np
import pytest

from hogline.perception.hog_ray import fit_hog_ray
from hogline.perception.perspective_rays import HOG_FROM_TEE_M


def points():
    u = np.linspace(-1, 1, 81)
    return [
        {"t": 10 + i / 60, "world_m": [0.3 + 0.02 * s + 0.1 * s**2, HOG_FROM_TEE_M + s]}
        for i, s in enumerate(u)
    ]


def test_hog_tangent_uses_local_derivative_instead_of_incoming_chord():
    result = fit_hog_ray(points(), 34.7472)
    assert result["hog_x_m"] == pytest.approx(0.3)
    assert result["slope_transverse_per_forward"] == pytest.approx(0.02)
    assert result["target_tee_x_m"] == pytest.approx(0.3 + 0.02 * (34.7472 - HOG_FROM_TEE_M))
    assert result["crossing_seconds"] == pytest.approx(10 + 40 / 60)


def test_hog_tangent_robust_to_a_wrong_handle_detection():
    rows = points()
    rows[19]["world_m"][0] += 0.5
    result = fit_hog_ray(rows, 34.7472)
    assert result["fit_point_count"] == 80
    assert result["slope_transverse_per_forward"] == pytest.approx(0.02)


@pytest.mark.parametrize("rows", [points()[:40], points()[42:], points()[::10]])
def test_hog_tangent_requires_dense_observations_on_both_sides(rows):
    with pytest.raises(ValueError):
        fit_hog_ray(rows, 34.7472)


def test_hog_tangent_rejects_occluded_crossing():
    rows = points()[:20] + points()[60:]
    with pytest.raises(ValueError, match="gap"):
        fit_hog_ray(rows, 34.7472)


def test_hog_direction_mirrors_for_opposite_transverse_coordinates():
    rows = points()
    mirrored = copy.deepcopy(rows)
    for p in mirrored:
        p["world_m"][0] *= -1
    assert fit_hog_ray(mirrored, 34.7472)["target_tee_x_m"] == pytest.approx(
        -fit_hog_ray(rows, 34.7472)["target_tee_x_m"]
    )


def test_saved_hog_requires_checksum_and_current_ray(db):
    import json

    from hogline.adapters.sources import digest
    from hogline.orchestration.hog_rays import key, saved

    ray = {"id": "native", "version": "original"}
    root = db.settings.data_dir / "hog-rays"
    root.mkdir()
    result = {"status": "unavailable", "error": "Occluded", "ray_sha256": digest(ray)}
    path = root / (key(ray) + ".json")
    path.write_text(json.dumps({**result, "sha256": digest(result)}))
    assert saved(db, ray) == result
    assert saved(db, {**ray, "version": "changed"}) is None
    path.write_text(path.read_text().replace("Occluded", "Changed"))
    assert saved(db, ray) is None
    path.write_text("{")
    assert saved(db, ray) is None


def test_stale_report_withholds_hog_measurement(db, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from hogline.review import hog_rays

    monkeypatch.setattr(hog_rays, "estimate_report", lambda *_: {"stale": True})
    app = FastAPI()
    hog_rays.register_hog_rays(app, db)
    response = TestClient(app).post("/api/games/game/hog-rays/shot")
    assert response.status_code == 409
