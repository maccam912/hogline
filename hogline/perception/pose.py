"""Image-space limb cues for delivery association, without identity or metric claims."""

import hashlib
import os
from pathlib import Path

import cv2
import numpy as np

VERSION = "mediapipe-body-limbs-v1"
MODEL_SHA256 = "9d89c599319a18fb7d2e28451a883476164543182bafca5f09eb2cf767ed2f3f"
MODEL_BYTES = 5557238
BODY_INDICES = tuple(range(11, 33))


def decode_pose(outputs, box, image_size):
    arrays = {tuple(np.asarray(a).shape): np.asarray(a) for a in outputs}
    if (1, 1) not in arrays or (1, 195) not in arrays:
        raise ValueError("Body-pose output does not match the pinned model contract")
    confidence = float(arrays[(1, 1)][0, 0])
    landmarks = arrays[(1, 195)][0].reshape(39, 5)
    if not np.isfinite(landmarks).all() or not np.isfinite(confidence) or not 0 <= confidence <= 1:
        raise ValueError("Body-pose output contains invalid coordinates or scores")
    if confidence < 0.5:
        return {
            "status": "unavailable",
            "missing_reason": "weak_body_pose",
            "raw_model_score": confidence,
            "landmarks": [],
        }
    x, y, width, height = box
    side = 1.3 * max(width, height)
    coordinates = (landmarks[:, :2] - 128) * side / 256 + [x + width / 2, y + height / 2]
    visibility = 1 / (1 + np.exp(-np.clip(landmarks[:, 3:], -30, 30)))
    torso = coordinates[[11, 12, 23, 24]].mean(axis=0)
    if not (
        x - width * 0.1 <= torso[0] <= x + width * 1.1
        and y - height * 0.1 <= torso[1] <= y + height * 1.1
        and np.mean(visibility[[11, 12, 23, 24], 1]) >= 0.5
    ):
        return {
            "status": "unavailable",
            "missing_reason": "pose_subject_alignment_unresolved",
            "raw_model_score": confidence,
            "landmarks": [],
        }
    w, h = image_size
    return {
        "status": "observed",
        "missing_reason": None,
        "raw_model_score": confidence,
        "landmarks": [
            {
                "index": i,
                "center_px": coordinates[i].astype(float).tolist(),
                "raw_visibility": float(visibility[i, 0]),
                "raw_presence": float(visibility[i, 1]),
                "inside_crop": bool(0 <= coordinates[i, 0] < w and 0 <= coordinates[i, 1] < h),
            }
            for i in BODY_INDICES
        ],
    }


class BodyPoseDetector:
    version = VERSION

    def __init__(self, path):
        if (
            path.stat().st_size != MODEL_BYTES
            or hashlib.sha256(path.read_bytes()).hexdigest() != MODEL_SHA256
        ):
            raise ValueError("Body-pose model checksum mismatch")
        self.net = cv2.dnn.readNet(str(path))

    def detect(self, rgb, box):
        x, y, width, height = box
        side = 1.3 * max(width, height)
        if min(width, height) < 16 or side > 2 * max(rgb.shape[:2]):
            return {
                "status": "unavailable",
                "missing_reason": "body_too_small_or_clipped",
                "landmarks": [],
                "raw_model_score": None,
            }
        scale = 256 / side
        matrix = np.array(
            [[scale, 0, 128 - (x + width / 2) * scale], [0, scale, 128 - (y + height / 2) * scale]],
            dtype=np.float32,
        )
        crop = cv2.warpAffine(rgb, matrix, (256, 256), borderMode=cv2.BORDER_CONSTANT)
        self.net.setInput(crop.astype(np.float32)[None] / 255)
        outputs = self.net.forward(self.net.getUnconnectedOutLayersNames())
        return decode_pose(outputs, box, (rgb.shape[1], rgb.shape[0]))


def optional_body_pose(root):
    path = Path(
        os.environ.get(
            "HOGLINE_POSE_MODEL", root / "models/mediapipe-pose/pose_estimation_mediapipe_2023mar.onnx"
        )
    )
    if not path.is_file():
        return None, {
            "status": "unavailable",
            "missing_reason": "pinned_body_pose_model_not_installed",
            "version": VERSION,
        }
    try:
        return BodyPoseDetector(path), {
            "status": "available",
            "version": VERSION,
            "model_sha256": MODEL_SHA256,
        }
    except (ValueError, cv2.error, OSError):
        return None, {
            "status": "unavailable",
            "version": VERSION,
            "missing_reason": "body_pose_model_invalid_or_unloadable",
        }
