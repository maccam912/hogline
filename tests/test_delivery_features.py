import copy

import cv2
import numpy as np
import pytest

from hogline.perception.perspective_stones import perspective_handle_tracks, perspective_handles
from hogline.perception.pose import BodyPoseDetector, decode_pose, optional_body_pose


def pose_outputs(confidence=0.9):
    points = np.full((39, 5), 128, dtype=np.float32)
    points[:, 2] = 0
    points[:, 3:] = 10
    points[15, :2] = [100, 190]
    return [points.reshape(1, 195), np.array([[confidence]], dtype=np.float32)]


def test_pose_preserves_only_body_joints_in_original_crop_pixels():
    box = [100, 150, 80, 160]
    pose = decode_pose(pose_outputs(), box, (500, 500))
    assert pose["status"] == "observed"
    assert [p["index"] for p in pose["landmarks"]] == list(range(11, 33))
    wrist = next(p for p in pose["landmarks"] if p["index"] == 15)
    assert wrist["center_px"] == pytest.approx([117.25, 280.375])
    assert wrist["inside_crop"] and wrist["raw_presence"] > 0.99

    class Network:
        def setInput(self, blob):
            self.blob = blob

        def getUnconnectedOutLayersNames(self):
            return ["points", "score"]

        def forward(self, names):
            return pose_outputs()

    detector = BodyPoseDetector.__new__(BodyPoseDetector)
    detector.net = Network()
    rgb = np.full((500, 500, 3), [230, 20, 20], dtype=np.uint8)
    result = detector.detect(rgb, box)
    assert result == pose
    assert detector.net.blob.shape == (1, 256, 256, 3)
    assert detector.net.blob[0, 128, 128].tolist() == pytest.approx([230 / 255, 20 / 255, 20 / 255])


def test_weak_misaligned_or_missing_pose_abstains(tmp_path, monkeypatch):
    assert decode_pose(pose_outputs(0.1), [100, 150, 80, 160], (500, 500))["status"] == "unavailable"
    invalid = pose_outputs()
    invalid[0][0, 0] = float("nan")
    with pytest.raises(ValueError, match="invalid"):
        decode_pose(invalid, [100, 150, 80, 160], (500, 500))
    shifted = pose_outputs()
    shifted[0][0, :] = 10000
    assert decode_pose(shifted, [100, 150, 80, 160], (500, 500))["status"] == "unavailable"
    monkeypatch.delenv("HOGLINE_POSE_MODEL", raising=False)
    assert optional_body_pose(tmp_path)[1]["status"] == "unavailable"
    model = tmp_path / "wrong.onnx"
    model.write_bytes(b"not a model")
    monkeypatch.setenv("HOGLINE_POSE_MODEL", str(model))
    assert optional_body_pose(tmp_path)[1]["missing_reason"] == "body_pose_model_invalid_or_unloadable"


def scene(i=0):
    image = np.full((500, 300, 3), 245, dtype=np.uint8)
    # A moving red handle on gray granite and a misleading red patch inside a person's upper body.
    for center in ((60 + i * 5, 300), (200, 220)):
        cv2.circle(image, center, 15, (130, 130, 130), -1)
        cv2.circle(image, center, 6, (230, 20, 20), -1)
    return image


def test_perspective_proposals_preserve_ambiguous_person_overlap_but_exclude_it_from_tracks():
    body = {"bbox_px": [170, 180, 60, 150]}
    observation = perspective_handles(scene(), [body])
    assert len(observation["detections"]) == 2
    overlapping = next(d for d in observation["detections"] if d["person_overlap"])
    assert overlapping["center_px"] == [200, 220] and not overlapping["near_wrist"]
    rows = []
    for i in range(6):
        rows.append(
            {
                "sample_index": i,
                "view": "perspective_right",
                "source_seconds": i * 0.5,
                "pts": i,
                "time_base_num": 1,
                "time_base_den": 2,
                "decoded_rgb_sha256": f"image-{i}",
                "youtube_url": None,
                "crop_width": 300,
                "crop_height": 500,
                "requested_interval_seconds": 0.5,
                "perspective_handles": perspective_handles(scene(i), [body]),
            }
        )
    tracks = perspective_handle_tracks(rows, "input")["tracks"]
    assert len(tracks) == 1 and tracks[0]["color"] == "red"
    assert len(tracks[0]["points"]) == 6
    assert tracks[0]["points"][0]["raw_detection_index"] == 1
    assert tracks[0]["points"][0]["candidate_bbox_px"] == [54, 294, 13, 13]
    assert tracks[0]["speed_meters_per_second"] is None
    held = copy.deepcopy(body)
    held["body_pose"] = {
        "landmarks": [
            {
                "index": 15,
                "center_px": [200, 220],
                "inside_crop": True,
                "raw_presence": 0.9,
                "raw_visibility": 0.9,
            }
        ]
    }
    held_reading = perspective_handles(scene(), [held])
    assert next(d for d in held_reading["detections"] if d["person_overlap"])["near_wrist"]
