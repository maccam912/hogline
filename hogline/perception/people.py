"""Optional pinned COCO person proposals for perspective camera actor tracking."""

import hashlib
import os
from pathlib import Path

import cv2
import numpy as np

VERSION = "yolox-perspective-people-v1"
MODEL_SHA256 = "c5c2d13e59ae883e6af3b45daea64af4833a4951c92d116ec270d9ddbe998063"
MODEL_BYTES = 35858002
MODEL_REFERENCE = (
    "https://github.com/opencv/opencv_zoo/tree/48b10fa505aeb51fbd127fc60a5c660040e823b7/"
    "models/object_detection_yolox"
)
MAX_PEOPLE = 64


def torso_descriptor(rgb, box):
    """Clothing color only; omit the head and never assign a named identity."""
    x, y, width, height = box
    h, w = rgb.shape[:2]
    x1, x2 = max(0, round(x + width * 0.2)), min(w, round(x + width * 0.8))
    y1, y2 = max(0, round(y + height * 0.25)), min(h, round(y + height * 0.65))
    if x2 - x1 < 4 or y2 - y1 < 4:
        return None
    hsv = cv2.cvtColor(rgb[y1:y2, x1:x2], cv2.COLOR_RGB2HSV)
    histogram = cv2.calcHist([hsv], [0, 1], None, [8, 4], [0, 180, 0, 256]).reshape(-1)
    return (histogram / max(1, histogram.sum())).astype(float).round(6).tolist()


def decode_people(outputs, rgb, ratio, threshold=0.35):
    predictions = np.asarray(outputs, dtype=np.float32)
    if predictions.shape != (1, 8400, 85) or not np.isfinite(predictions).all():
        raise ValueError("Person network output does not match the pinned model contract")
    predictions = predictions[0]
    if np.any(predictions[:, 4:] < 0) or np.any(predictions[:, 4:] > 1):
        raise ValueError("Person network scores do not match the pinned model contract")
    grids, strides = [], []
    for stride in (8, 16, 32):
        y, x = np.mgrid[: 640 // stride, : 640 // stride]
        grids.append(np.stack([x, y], axis=-1).reshape(-1, 2))
        strides.append(np.full((x.size, 1), stride))
    grid, stride = np.concatenate(grids), np.concatenate(strides)
    centers = (predictions[:, :2] + grid) * stride
    sizes = np.exp(np.clip(predictions[:, 2:4], -10, 10)) * stride
    boxes = np.concatenate([centers - sizes / 2, sizes], axis=1) / ratio
    scores = predictions[:, 4] * predictions[:, 5]  # COCO person class, uncalibrated.
    selected = np.flatnonzero((scores >= threshold) & (np.argmax(predictions[:, 5:], axis=1) == 0))
    if not len(selected):
        return []
    keep = cv2.dnn.NMSBoxes(boxes[selected].tolist(), scores[selected].tolist(), threshold, 0.5)
    h, w = rgb.shape[:2]
    detections = []
    for k in np.asarray(keep).reshape(-1):
        index = selected[k]
        x, y, width, height = boxes[index]
        x1, y1, x2, y2 = max(0, x), max(0, y), min(w, x + width), min(h, y + height)
        if x2 - x1 < 4 or y2 - y1 < 4:
            continue  # Padding-only or tiny boxes cannot carry an actor association.
        box = [float(x1), float(y1), float(x2 - x1), float(y2 - y1)]
        detections.append(
            {
                "bbox_px": box,
                "raw_model_score": float(scores[index]),
                "score_kind": "uncalibrated_objectness_times_person_class",
                "boundary_clipped": bool(x < 0 or y < 0 or x + width > w or y + height > h),
                "torso_hsv_histogram": torso_descriptor(rgb, box),
            }
        )
    if len(detections) > MAX_PEOPLE:
        raise ValueError("Person candidate count exceeds the bounded model contract")
    return sorted(detections, key=lambda d: (-d["raw_model_score"], d["bbox_px"]))


class PersonDetector:
    version = VERSION

    def __init__(self, path):
        if (
            path.stat().st_size != MODEL_BYTES
            or hashlib.sha256(path.read_bytes()).hexdigest() != MODEL_SHA256
        ):
            raise ValueError("Person model checksum mismatch")
        self.net = cv2.dnn.readNet(str(path))

    def detect(self, rgb):
        height, width = rgb.shape[:2]
        ratio = min(640 / height, 640 / width)
        resized = cv2.resize(rgb, (int(width * ratio), int(height * ratio)))
        padded = np.full((640, 640, 3), 114, dtype=np.float32)
        padded[: resized.shape[0], : resized.shape[1]] = resized
        self.net.setInput(padded.transpose(2, 0, 1)[None])
        return decode_people(self.net.forward(), rgb, ratio)


def optional_person_detector(root):
    path = Path(
        os.environ.get(
            "HOGLINE_PERSON_MODEL", root / "models/yolox-s-coco/object_detection_yolox_2022nov.onnx"
        )
    )
    if not path.is_file():
        return None, {"status": "unavailable", "missing_reason": "pinned_person_model_not_installed"}
    try:
        return PersonDetector(path), {"status": "available", "model_sha256": MODEL_SHA256}
    except (ValueError, cv2.error, OSError):
        return None, {"status": "unavailable", "missing_reason": "pinned_person_model_invalid_or_unloadable"}
