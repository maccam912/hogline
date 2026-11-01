"""Operator-requested, local face observations for game lineups.

Faces are only sought around the heads of person boxes standing on the ice. Templates
never leave the local biometric store; names are attached later by an operator.
"""

import hashlib
import os
import shutil
import tempfile
import urllib.request
import zipfile
from pathlib import Path

import cv2
import numpy as np

VERSION = "on-ice-face-observations-v1"
ALIGNED_SIZE = 112
THUMB_SIZE = 64
MAX_FACES_PER_VIEW = 24
TARGET_FACE_PX = 48
MAX_UPSCALE = 4.0
MAX_PATCH_SIDE = 1280
# Standard five-point ArcFace template; YuNet emits landmarks in the same order.
TEMPLATE = np.array(
    [[38.2946, 51.6963], [73.5318, 51.5014], [56.0252, 71.7366], [41.5493, 92.3655], [70.7299, 92.2041]],
    dtype=np.float32,
)

ZOO = "https://github.com/opencv/opencv_zoo/raw/48b10fa505aeb51fbd127fc60a5c660040e823b7/models"
MODELS = {
    "yunet": {
        "role": "detector",
        "file": "face-yunet/face_detection_yunet_2023mar.onnx",
        "bytes": 232589,
        "sha256": "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4",
        "url": f"{ZOO}/face_detection_yunet/face_detection_yunet_2023mar.onnx",
        "license": "MIT (OpenCV Zoo YuNet)",
    },
    "arcface-r50": {
        "role": "embedder",
        "file": "face-arcface-r50/w600k_r50.onnx",
        "bytes": 174383860,
        "sha256": "4c06341c33c2ca1f86781dab0e829f88ad5b64be9fba56e56bc9ebdefc619e43",
        "url": "https://github.com/deepinsight/insightface/releases/download/v0.7/buffalo_l.zip",
        "archive_member": "w600k_r50.onnx",
        "dimensions": 512,
        "license": "InsightFace buffalo_l weights: non-commercial research use only",
    },
    "sface": {
        "role": "embedder",
        "file": "face-sface/face_recognition_sface_2021dec.onnx",
        "bytes": 38696353,
        "sha256": "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79",
        "url": f"{ZOO}/face_recognition_sface/face_recognition_sface_2021dec.onnx",
        "dimensions": 128,
        "license": "Apache-2.0 (OpenCV Zoo SFace)",
    },
}
EMBEDDER_PREFERENCE = ("arcface-r50", "sface")


def model_path(root, key):
    override = os.environ.get(f"HOGLINE_FACE_MODEL_{key.upper().replace('-', '_')}")
    from hogline.config import pretrained_model_path

    return Path(override) if override else pretrained_model_path(root, MODELS[key]["file"])


_VERIFIED = {}


def verified(path, key):
    """Size + SHA-256 check, remembered per file identity so schedulers can poll cheaply."""
    spec = MODELS[key]
    path = Path(path)
    try:
        stat = path.stat()
    except OSError:
        return False
    if not path.is_file() or stat.st_size != spec["bytes"]:
        return False
    identity = (str(path), key, stat.st_size, stat.st_mtime_ns, stat.st_ino)
    if identity not in _VERIFIED:
        with path.open("rb") as handle:
            _VERIFIED[identity] = hashlib.file_digest(handle, "sha256").hexdigest() == spec["sha256"]
    return _VERIFIED[identity]


def face_model_status(root):
    return {
        key: {
            "path": str(model_path(root, key)),
            "installed": verified(model_path(root, key), key),
            "role": spec["role"],
            "license": spec["license"],
        }
        for key, spec in MODELS.items()
    }


def install_face_models(root, keys=("yunet", "arcface-r50", "sface")):
    """Explicit operator download with pinned sizes and checksums; never called implicitly."""
    installed = {}
    for key in keys:
        spec, target = MODELS[key], model_path(root, key)
        if verified(target, key):
            installed[key] = "already_installed"
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=target.parent) as temp:
            download = Path(temp) / "download"
            with urllib.request.urlopen(spec["url"], timeout=60) as response, download.open("wb") as out:
                shutil.copyfileobj(response, out, 1024**2)
            candidate = download
            if spec.get("archive_member"):
                with zipfile.ZipFile(download) as archive:
                    candidate = Path(archive.extract(spec["archive_member"], temp))
            if not verified(candidate, key):
                raise ValueError(f"Downloaded {key} model does not match its pinned size and checksum")
            candidate.replace(target)
        installed[key] = "installed"
    return installed


def head_patch(box, width, height):
    """Search region and upscale factor for one body box (crop pixels)."""
    x, y, w, h = box
    if w <= 0 or h <= 0:
        return None
    if w / h >= 0.75:  # Crouched delivery or sweeping posture: the head can be anywhere in the box.
        x0, x1, y0, y1, face = x - 0.1 * w, x + 1.1 * w, y - 0.1 * h, y + 1.0 * h, 0.22 * h
    else:
        x0, x1, y0, y1, face = x - 0.15 * w, x + 1.15 * w, y - 0.08 * h, y + 0.4 * h, h / 7.5
    x0, y0 = max(0, int(np.floor(x0))), max(0, int(np.floor(y0)))
    x1, y1 = min(width, int(np.ceil(x1))), min(height, int(np.ceil(y1)))
    if x1 - x0 < 4 or y1 - y0 < 4:
        return None
    scale = float(np.clip(TARGET_FACE_PX / max(face, 1.0), 1.0, MAX_UPSCALE))
    scale = min(scale, MAX_PATCH_SIDE / max(x1 - x0, y1 - y0))
    return (x0, y0, x1, y1), max(1.0, scale)


def align(image_bgr, landmarks):
    matrix, _ = cv2.estimateAffinePartial2D(
        np.asarray(landmarks, dtype=np.float32).reshape(5, 2), TEMPLATE, method=cv2.LMEDS
    )
    if matrix is None or not np.isfinite(matrix).all():
        return None
    return cv2.warpAffine(image_bgr, matrix, (ALIGNED_SIZE, ALIGNED_SIZE), borderValue=(0, 0, 0))


def box_iou(a, b):
    ox = max(0.0, min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0]))
    oy = max(0.0, min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1]))
    overlap = ox * oy
    return overlap / max(1e-9, a[2] * a[3] + b[2] * b[3] - overlap)


def on_ice(person, height, min_foot_y):
    x, y, w, h = person["bbox_px"]
    return (y + h) / height >= min_foot_y


def installed_face_version(root, embedder_key=None):
    """Check pinned files without loading networks; returns (embedder, version)."""
    message = "Face models are not installed. Run `hogline face-models --install` (explicit download)."
    if not verified(model_path(root, "yunet"), "yunet"):
        raise ValueError(message)
    keys = [embedder_key] if embedder_key else EMBEDDER_PREFERENCE
    key = next((k for k in keys if verified(model_path(root, k), k)), None)
    if key is None:
        raise ValueError(message)
    return key, f"{VERSION}:yunet:{key}:{MODELS[key]['sha256'][:16]}"


class FaceModels:
    def __init__(self, root, embedder_key=None):
        detector_path = model_path(root, "yunet")
        if not verified(detector_path, "yunet"):
            raise FileNotFoundError("Pinned YuNet face detector is not installed")
        keys = [embedder_key] if embedder_key else EMBEDDER_PREFERENCE
        self.embedder_key = next((k for k in keys if verified(model_path(root, k), k)), None)
        if self.embedder_key is None:
            raise FileNotFoundError("No pinned face embedder is installed")
        self.detector = cv2.FaceDetectorYN.create(str(detector_path), "", (320, 320), 0.6, 0.3, 20)
        if self.embedder_key == "arcface-r50":
            self.net = cv2.dnn.readNetFromONNX(str(model_path(root, "arcface-r50")))
        else:
            self.recognizer = cv2.FaceRecognizerSF.create(str(model_path(root, "sface")), "")
        self.dimensions = MODELS[self.embedder_key]["dimensions"]
        self.version = f"{VERSION}:yunet:{self.embedder_key}:{MODELS[self.embedder_key]['sha256'][:16]}"

    def info(self):
        return {
            "status": "available",
            "version": self.version,
            "detector": "yunet",
            "detector_sha256": MODELS["yunet"]["sha256"],
            "embedder": self.embedder_key,
            "embedder_sha256": MODELS[self.embedder_key]["sha256"],
            "dimensions": self.dimensions,
            "license": MODELS[self.embedder_key]["license"],
        }

    def detect(self, patch_bgr):
        self.detector.setInputSize((patch_bgr.shape[1], patch_bgr.shape[0]))
        _, found = self.detector.detect(patch_bgr)
        return [] if found is None else [np.asarray(f, dtype=np.float32) for f in found]

    def embed(self, aligned_bgr):
        if not aligned_bgr:
            return np.zeros((0, self.dimensions), dtype=np.float32)
        out = []
        for start in range(0, len(aligned_bgr), 64):
            batch = aligned_bgr[start : start + 64]
            if self.embedder_key == "arcface-r50":
                blob = cv2.dnn.blobFromImages(
                    batch, 1 / 127.5, (112, 112), (127.5, 127.5, 127.5), swapRB=True
                )
                self.net.setInput(blob)
                vectors = self.net.forward().copy()
                self.net.setInput(np.ascontiguousarray(blob[:, :, :, ::-1]))
                vectors += self.net.forward()
            else:
                vectors = np.stack(
                    [
                        self.recognizer.feature(a).reshape(-1)
                        + self.recognizer.feature(cv2.flip(a, 1)).reshape(-1)
                        for a in batch
                    ]
                )
            out.append(vectors)
        vectors = np.concatenate(out).astype(np.float32)
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        if not np.isfinite(vectors).all() or (norms <= 0).any():
            raise ValueError("Face embedder returned invalid vectors")
        return vectors / norms

    def observe(self, rgb, people, *, min_eye_px=6.0, min_score=0.7, min_foot_y=0.4):
        """Return quality-gated faces attached to on-ice people, plus counts for abstentions."""
        height, width = rgb.shape[:2]
        bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
        counts = {"people": len(people), "off_ice_people": 0, "no_face": 0, "below_quality": 0}
        candidates = []
        for index, person in enumerate(people):
            if not on_ice(person, height, min_foot_y):
                counts["off_ice_people"] += 1
                continue
            px, py, pw, ph = person["bbox_px"]
            region = head_patch(person["bbox_px"], width, height)
            if region is None:
                counts["no_face"] += 1
                continue
            (x0, y0, x1, y1), scale = region
            patch = cv2.resize(
                bgr[y0:y1, x0:x1],
                (max(1, round((x1 - x0) * scale)), max(1, round((y1 - y0) * scale))),
                interpolation=cv2.INTER_CUBIC,
            )
            best = None
            for found in self.detect(patch):
                fx, fy, fw, fh = (float(v) for v in found[:4])
                cx, cy = x0 + (fx + fw / 2) / scale, y0 + (fy + fh / 2) / scale
                if not px - 0.05 * pw <= cx <= px + 1.05 * pw:
                    continue
                if pw / ph < 0.75 and cy > py + 0.35 * ph:
                    continue
                if best is None or found[14] > best[14]:
                    best = found
            if best is None:
                counts["no_face"] += 1
                continue
            landmarks = best[4:14].reshape(5, 2)
            eye_px = float(np.linalg.norm(landmarks[0] - landmarks[1]) / scale)
            if float(best[14]) < min_score or eye_px < min_eye_px:
                counts["below_quality"] += 1
                continue
            aligned = align(patch, best[4:14])
            if aligned is None:
                counts["no_face"] += 1
                continue
            gray = cv2.cvtColor(aligned, cv2.COLOR_BGR2GRAY)
            mid = (landmarks[0] + landmarks[1]) / 2
            eye_gap = max(1e-6, float(np.linalg.norm(landmarks[0] - landmarks[1])))
            candidates.append(
                {
                    "person_index": index,
                    "bbox_px": [
                        x0 + float(best[0]) / scale,
                        y0 + float(best[1]) / scale,
                        float(best[2]) / scale,
                        float(best[3]) / scale,
                    ],
                    "person_bbox_px": [float(v) for v in person["bbox_px"]],
                    "raw_detector_score": float(best[14]),
                    "eye_px": eye_px,
                    "yaw_proxy": float((landmarks[2, 0] - mid[0]) / eye_gap),
                    "sharpness": float(cv2.Laplacian(gray, cv2.CV_64F).var()),
                    "foot_x": (px + pw / 2) / width,
                    "foot_y": (py + ph) / height,
                    "torso_hsv_histogram": person.get("torso_hsv_histogram"),
                    "_aligned": aligned,
                }
            )
        # One face per head: overlapping bodies can propose the same face twice.
        candidates.sort(key=lambda c: -c["raw_detector_score"])
        kept = []
        for c in candidates:
            if all(box_iou(c["bbox_px"], k["bbox_px"]) < 0.3 for k in kept):
                kept.append(c)
        counts["duplicate_faces"] = len(candidates) - len(kept)
        kept = sorted(kept, key=lambda c: -c["eye_px"])[:MAX_FACES_PER_VIEW]
        vectors = self.embed([c["_aligned"] for c in kept])
        for c, vector in zip(kept, vectors, strict=True):
            aligned = c.pop("_aligned")
            c["embedding"] = vector
            c["thumbnail"] = cv2.cvtColor(
                cv2.resize(aligned, (THUMB_SIZE, THUMB_SIZE), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2RGB
            )
        counts["faces"] = len(kept)
        return kept, counts


def optional_face_models(root, embedder_key=None):
    try:
        models = FaceModels(root, embedder_key)
    except FileNotFoundError as exc:
        return None, {"status": "unavailable", "missing_reason": str(exc)}
    except (cv2.error, ValueError) as exc:
        return None, {"status": "unavailable", "missing_reason": f"face model failed to load: {exc}"}
    return models, models.info()
