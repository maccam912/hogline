import numpy as np
import pytest

from hogline.perception.projective_house import diagnose_house_projection, project_points


def projected_paint(*, obstruct=False, green_radius=1 / 3):
    y, x = np.mgrid[:480, :480]
    x, y = (x - 240) / 215, (y - 240) / 215
    z = 1 + 0.03 * x - 0.025 * y
    u, v = (1.02 * x + 0.025) / z, (0.02 * x + 0.99 * y - 0.02) / z
    radius = np.sqrt(u**2 + v**2)
    image = np.full((480, 480, 3), 225, dtype=np.uint8)
    image[(radius < 1) & (radius > 2 / 3)] = [30, 45, 190]
    image[(radius < green_radius) & (radius > 1 / 9)] = [30, 160, 90]
    if obstruct:
        image[70:230, 50:180] = 30
        image[20:100, 330:455] = [30, 45, 190]
    return image


@pytest.mark.parametrize("obstruct", [False, True])
def test_projected_paint_recovers_tee_and_held_out_geometry_with_occlusion_and_colored_outliers(obstruct):
    report = diagnose_house_projection(projected_paint(obstruct=obstruct))
    assert report["status"] == "candidate", report
    assert all(24 <= ring["angular_sectors_of_36"] <= 36 for ring in report["ring_diagnostics"])
    expected = np.linalg.solve([[1.02, 0], [0.02, 0.99]], [-0.025, 0.02]) * 215 + 240
    assert np.linalg.norm(np.array(report["tee_px"]) - expected) < 1.5
    # These points lie on the known painted geometry, separate from the fitted samples.
    angles = np.arange(0.1, 6.2, 0.3)
    canonical = np.column_stack((np.cos(angles), np.sin(angles))) * 0.5
    forward = np.array([[1.02, 0, 0.025], [0.02, 0.99, -0.02], [0.03, -0.025, 1]])
    pixels = np.column_stack((canonical, np.ones(len(canonical)))) @ np.linalg.inv(forward).T
    pixels = pixels[:, :2] / pixels[:, 2:] * 215 + 240
    projected = project_points(
        pixels, report["pixel_normalized_to_house"], report["origin_px"], report["scale_px"]
    )
    assert np.max(np.abs(np.linalg.norm(projected, axis=1) - 0.5)) < 0.01
    assert report["provisional_score"] is None and report["metric_calibration"] is None


def test_missing_rings_inconsistent_ratios_and_insufficient_angles_abstain():
    blank = np.full((480, 480, 3), 225, dtype=np.uint8)
    assert diagnose_house_projection(blank)["status"] == "unavailable"
    assert diagnose_house_projection(projected_paint(green_radius=0.47))["status"] == "unavailable"
    half = projected_paint()
    half[:260] = 30
    assert diagnose_house_projection(half)["status"] != "candidate"


def test_non_rgb_and_out_of_scope_inputs_are_rejected():
    with pytest.raises(ValueError, match="RGB uint8"):
        diagnose_house_projection(np.zeros((200, 200)))
    with pytest.raises(ValueError, match="native house crop"):
        diagnose_house_projection(np.zeros((20, 20, 3), dtype=np.uint8))
    with pytest.raises(ValueError, match="finite two-dimensional"):
        project_points([[float("nan"), 0]], np.eye(3), [0, 0], 1)


def test_cli_records_exact_input_and_preserves_existing_diagnostic(tmp_path, monkeypatch, capsys):
    import hashlib
    import json
    import sys

    from PIL import Image

    from hogline.cli import main

    path, output = tmp_path / "native.png", tmp_path / "projection.json"
    Image.fromarray(projected_paint(obstruct=True)).save(path)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "hogline",
            "--data-dir",
            str(tmp_path / "archive"),
            "diagnose-house-projection",
            str(path),
            "--output",
            str(output),
        ],
    )
    main()
    report = json.loads(output.read_text())
    assert report["input"]["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert report["status"] == "candidate" and report["provisional_score"] is None
    before = output.read_bytes()
    with pytest.raises(SystemExit):
        main()
    assert output.read_bytes() == before
    assert "destination exists" in capsys.readouterr().err
