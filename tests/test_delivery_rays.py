import subprocess

import numpy as np
import pytest

from hogline.media.frames import capture
from hogline.media.windows import decode_native_crop
from hogline.perception.delivery_ray import RADIUS_M, TEE_DISTANCE_M, fit_ray, ice_points, image_points


def geometry():
    return {
        "projection": {
            "pixel_normalized_to_house": np.eye(3).tolist(),
            "origin_px": [160, 160],
            "scale_px": 140,
        },
        "rotation": np.eye(2).tolist(),
    }


def track(sign=1, start=-2, stop=2):
    s = np.linspace(start, stop, 101)
    xy = np.column_stack((0.1 + 0.02 * s, sign * s))
    pixels = image_points(xy, geometry())
    return [{"pixel": p.tolist(), "t": 10 + i / 60, "pts": 900000 + i * 1500} for i, p in enumerate(pixels)]


@pytest.mark.parametrize("sign", [-1, 1])
def test_ray_transfers_same_x_handedness_and_retains_native_timing(sign):
    r = fit_ray(track(sign), geometry(), sign)
    assert r["target_tee_x_m"] == pytest.approx(0.1 + 0.02 * TEE_DISTANCE_M)
    assert r["near_hack_ray_x_m"] == pytest.approx(0.1 - 0.02 * 3.6576)
    assert r["house_split"]["status"] == "observed_full_house"
    assert r["house_split"]["elapsed_seconds"] == pytest.approx(2 * RADIUS_M / (4 / (100 / 60)))
    assert r["house_split"]["source_frame_difference"] == 91
    assert r["target_tee_sensitivity_m"][0] < r["target_tee_x_m"] < r["target_tee_sensitivity_m"][1]
    assert "final destination" in r["interpretation"]
    assert r.get("weight_prediction") is None


def test_projective_and_rotated_mapping_round_trips():
    g = geometry()
    g["projection"]["pixel_normalized_to_house"] = [[1, 0.03, 0.02], [0.02, 0.95, -0.01], [0.01, -0.02, 1]]
    angle = 0.04
    g["rotation"] = [[np.cos(angle), -np.sin(angle)], [np.sin(angle), np.cos(angle)]]
    xy = np.array([[0.3, -1.7], [-1.2, 0.5]])
    assert ice_points(image_points(xy, g), g) == pytest.approx(xy)


def test_small_crop_edge_gap_is_estimated_and_large_gap_stays_partial():
    r = fit_ray(track(start=-1.7, stop=1.7), geometry(), 1)
    assert r["house_split"]["status"] == "estimated_full_house"
    assert r["house_split"]["entry"]["extrapolated"]
    assert "bracket_seconds" not in r["house_split"]["entry"]
    assert r["house_split"]["elapsed_seconds"] == pytest.approx(2 * RADIUS_M / (3.4 / (100 / 60)))
    r = fit_ray(track(start=-1, stop=1), geometry(), 1)
    assert r["house_split"]["status"] == "partial_house_only"
    assert "elapsed_seconds" not in r["house_split"]


def test_ray_rejects_missing_or_reversed_times_and_short_paths():
    points = track()
    points[20]["t"] = points[19]["t"]
    with pytest.raises(ValueError, match="timing reversal"):
        fit_ray(points, geometry(), 1)
    with pytest.raises(ValueError, match="Insufficient"):
        fit_ray(track(start=0, stop=0.5), geometry(), 1)
    points = track()
    points[50]["t"] += 1
    with pytest.raises(ValueError, match="timing reversal"):
        fit_ray(points, geometry(), 1)


@pytest.mark.parametrize("fps", [0, -1, 61, float("nan")])
def test_native_decoder_rejects_bad_cadence_before_decoding(fps):
    with pytest.raises(ValueError, match="Invalid native"):
        list(decode_native_crop("unused", 0, 1, [0, 0, 100, 100], fps=fps))


def test_native_crop_preserves_all_60hz_source_pts_and_exact_pixels(tmp_path):
    video = tmp_path / "sixty.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=160x90:rate=60",
            "-t",
            "1",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(video),
        ],
        check=True,
    )
    rows = list(decode_native_crop(str(video), 0.2, 0.4, [20, 10, 100, 80]))
    times = [t["pts"] * t["time_base_num"] / t["time_base_den"] for _, t in rows]
    assert times == pytest.approx([0.2 + i / 60 for i in range(12)])
    assert all(im.size == (80, 70) for im, _ in rows)
    original, tm = capture(str(video), times[5] - 0.00001)
    assert tm == rows[5][1]
    assert original.crop((20, 10, 100, 80)).tobytes() == rows[5][0].tobytes()


def test_varying_source_cadence_does_not_invent_frame_count():
    points = track()
    points[50]["t"] += 0.004
    with pytest.raises(ValueError, match="Variable source cadence"):
        fit_ray(points, geometry(), 1)


@pytest.mark.parametrize("sign", [-1, 1])
def test_svg_uses_native_background_and_true_ray_angle_without_flipping_x(db, sources, sign):
    import xml.etree.ElementTree as ET

    from PIL import Image

    from hogline.analytics.delivery_rays import ray_svg
    from hogline.db import Evidence
    from hogline.media.frames import save_image

    asset = save_image(Image.new("RGB", (320, 320), "white"), db.settings.data_dir)
    with db.snapshot_session(write=True) as session:
        session.add(
            Evidence(
                id="ray-frame",
                source_id=sources[0]["id"],
                evidence_key="ray-key",
                document={"crops": {"house_top": asset}},
            )
        )
    r = fit_ray(track(sign), geometry(), sign)
    r.update(
        origin_frame_id="ray-frame",
        receiving_frame_id="ray-frame",
        origin_view="house_top",
        receiving_view="house_top",
        geometry=geometry(),
        receiving_geometry=geometry(),
        observed_weight={"status": "unresolved"},
    )
    root = ET.fromstring(ray_svg(db, r, "receiving"))
    ns = {"s": "http://www.w3.org/2000/svg"}
    image = root.find("s:image", ns)
    assert image.attrib["href"].startswith("data:image/png;base64,")
    line = root.find("s:g/s:polyline", ns)
    xy = np.array([[float(z) for z in point.split(",")] for point in line.attrib["points"].split()])
    assert (xy[-1, 0] - xy[0, 0]) / (xy[-1, 1] - xy[0, 1]) == pytest.approx(0.02 * sign, abs=0.0001)
    dot = root.find("s:g/s:circle", ns)
    assert float(dot.attrib["cx"]) == pytest.approx(160 + r["target_tee_x_m"] / RADIUS_M * 140, abs=0.01)
    assert "Weight forecast unavailable" in ray_svg(db, r, "receiving")
