import cv2
import numpy as np
from PIL import Image

from hogline.perception.camera_registration import register_camera_images
from hogline.perception.landmark_calibration import map_points


def textured_image():
    rng = np.random.default_rng(423)
    return Image.fromarray(rng.integers(0, 255, (500, 500, 3), dtype=np.uint8))


def test_small_camera_drift_recovers_known_mapping_and_reports_held_out_error():
    original = textured_image()
    matrix = np.array([[1, 0, 3], [0, 1, -2], [0, 0, 1]], dtype=float)
    target = Image.fromarray(cv2.warpPerspective(np.asarray(original), matrix, original.size))
    result = register_camera_images(original, target)
    assert result["status"] == "alignment_proposal", result
    assert result["fit_inlier_count"] >= 24 and result["held_out_count"] >= 10
    pixels = [[70, 100], [400, 130], [300, 400]]
    assert np.max(np.abs(map_points(pixels, result["matrix"]) - map_points(pixels, matrix))) < 0.6
    assert result["acceptance"] == "proposed"


def test_different_or_textureless_cameras_abstain_and_large_shifts_are_not_reused():
    original = textured_image()
    assert register_camera_images(original, original.resize((400, 400)))["status"] == "unavailable"
    assert (
        register_camera_images(Image.new("RGB", original.size), original)["reason"]
        == "insufficient_camera_texture"
    )
    other = Image.fromarray(np.random.default_rng(4).integers(0, 255, (500, 500, 3), dtype=np.uint8))
    assert register_camera_images(original, other)["status"] == "unavailable"
    moved = Image.fromarray(
        cv2.warpPerspective(
            np.asarray(original), np.array([[1.0, 0, 70], [0, 1, 0], [0, 0, 1]]), original.size
        )
    )
    assert register_camera_images(original, moved)["reason"] == "camera_change_exceeds_small_drift_pilot"
