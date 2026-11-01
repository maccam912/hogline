import copy
import xml.etree.ElementTree as ET

import cv2
import numpy as np
import pytest
from PIL import Image

from hogline.perception.delivery_ray import TEE_DISTANCE_M
from hogline.perception.perspective_rays import fit_perspective, paired_distance, project, ray_segments


def camera(house, sign=1):
    h = np.array([[83, -5.5, 220], [0, 0.25, 310], [0, -0.025, 1]])
    return {
        "local_to_pixel": h.tolist(),
        "pixel_to_local": np.linalg.inv(h).tolist(),
        "image_size": [480, 684],
        "tee_px": [220, 310],
        "ice_bottom_px": 660,
        "house_view": house,
        "transverse_sign": sign,
    }


def pairs(cameras, distance=33.8):
    result = []
    for i, y in enumerate([12, 15, 18]):
        xy = {"perspective_left": [0.3, y + 0.145], "perspective_right": [-0.3, distance - y + 0.145]}
        result.append(
            {
                "source_seconds": 20 + i,
                **{v: project([p], cameras[v]["local_to_pixel"])[0].tolist() for v, p in xy.items()},
            }
        )
    return result


def test_paired_distance_is_independent_of_nominal_span_and_checks_mirrored_x():
    cameras = {"perspective_left": camera("house_top"), "perspective_right": camera("house_bottom", -1)}
    result = paired_distance(cameras, pairs(cameras))
    assert result["estimate_m"] == pytest.approx(33.8)
    assert result["estimate_m"] != pytest.approx(TEE_DISTANCE_M)
    assert not result["used_nominal_tee_distance_in_fit"]
    assert max(p["transverse_disagreement_m"] for p in result["pairs"]) < 1e-10
    bad = pairs(cameras)
    bad[0]["perspective_left"][0] += 20
    assert paired_distance(cameras, bad)["pairs"][0]["transverse_disagreement_m"] > 0.1


@pytest.mark.parametrize("origin", ["house_top", "house_bottom"])
@pytest.mark.parametrize("house,sign", [("house_top", 1), ("house_bottom", -1)])
def test_perspective_ray_uses_same_world_line_and_clips_camera_horizon(origin, house, sign):
    c = camera(house, sign)
    ray = {
        "nominal_tee_distance_m": TEE_DISTANCE_M,
        "near_tee_x_m": 0.15,
        "slope_transverse_per_forward": 0.018,
        "origin_view": origin,
    }
    segments = ray_segments(ray, c)
    assert segments
    for segment in segments:
        p = np.array(segment)
        assert np.isfinite(p).all()
        assert np.min(p[:, 1]) >= 270 and np.max(p[:, 1]) <= 660
        local = project(p, c["pixel_to_local"])
        s = local[:, 1] if origin == house else TEE_DISTANCE_M - local[:, 1]
        assert local[:, 0] * sign == pytest.approx(0.15 + 0.018 * s, abs=1e-9)


def test_perspective_calibration_checks_local_landmarks_without_114ft_constraint():
    c = camera("house_top")
    im = np.full((684 * 4, 480 * 4, 3), 195, dtype=np.uint8)
    h = np.array(c["local_to_pixel"])
    angles = np.linspace(0, 2 * np.pi, 1000)
    for radius, color in [(1.8288, (65, 120, 180)), (1.2192, (195, 195, 195)), (0.6096, (130, 185, 110))]:
        p = project(np.column_stack((radius * np.cos(angles), radius * np.sin(angles))), h)
        cv2.fillPoly(im, [np.round(4 * p).astype(np.int32)], color)
    for world in ([[0, 3], [0, 24]], [[-3, 6.4008], [3, 6.4008]]):
        p = np.round(project(world, h) * 4).astype(int)
        cv2.line(im, tuple(p[0]), tuple(p[1]), (145, 145, 145), 4)
    rgb = np.asarray(Image.fromarray(im).resize((480, 684), Image.Resampling.LANCZOS))
    hog_y = float(project([[0, 6.4008]], h)[0, 1])
    result = fit_perspective(
        rgb,
        {
            "tee_seed_px": [220, 310],
            "hog_y_seed_px": round(hog_y),
            "ice_bottom_px": 660,
            "house_view": "house_top",
            "transverse_sign": 1,
        },
    )
    assert result["held_out_ring_p90_px"] < 1.5
    assert project([[0, 0]], result["local_to_pixel"])[0] == pytest.approx([220, 310], abs=1)
    assert "nominal_tee_distance_m" not in result
    with pytest.raises(ValueError, match="painted blue"):
        fit_perspective(
            np.full_like(rgb, 195),
            {"tee_seed_px": [220, 310], "hog_y_seed_px": round(hog_y), "ice_bottom_px": 660},
        )


def test_perspective_svg_retains_native_image_dotted_ray_and_split_proxy(db, sources):
    from hogline.analytics.delivery_rays import ray_svg
    from hogline.db import Evidence
    from hogline.media.frames import save_image

    asset = save_image(Image.new("RGB", (480, 684), "white"), db.settings.data_dir)
    with db.snapshot_session(write=True) as session:
        session.add(
            Evidence(
                id="persp",
                source_id=sources[0]["id"],
                evidence_key="persp",
                document={"crops": {"perspective_right": asset}},
            )
        )
    ray = {
        "nominal_tee_distance_m": TEE_DISTANCE_M,
        "near_tee_x_m": 0.15,
        "slope_transverse_per_forward": 0.018,
        "origin_view": "house_bottom",
        "target_tee_x_m": 0.15 + 0.018 * TEE_DISTANCE_M,
        "perspectives": {"perspective_right": {"frame_id": "persp", "camera": camera("house_bottom", -1)}},
        "house_split": {"elapsed_seconds": 1.4, "nominal_mean_speed_mps": 3.6576 / 1.4},
    }
    svg = ray_svg(db, ray, "perspective_right")
    root = ET.fromstring(svg)
    ns = {"s": "http://www.w3.org/2000/svg"}
    assert root.find("s:image", ns).attrib["href"].startswith("data:image/png;base64,")
    assert root.find("s:g/s:polyline", ns).attrib["stroke-dasharray"] == "5 5"
    assert "Weight proxy: 1.400 s" in svg
    partial = copy.deepcopy(ray)
    partial["house_split"] = {"status": "partial_house_only"}
    assert "full split unavailable" in ray_svg(db, partial, "perspective_right")


def test_scope_check_precedes_decoding_another_recording(db):
    from hogline.orchestration.perspective_rays import prepare_perspectives

    with pytest.raises(ValueError, match="another recording"):
        prepare_perspectives(db, {"id": "other"}, {}, {"source_id": "reviewed"})
