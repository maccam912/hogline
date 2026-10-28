"""Local learned rejection of raw color proposals; still handle centers, not stone footprints."""

import hashlib

import cv2
import numpy as np

from hogline.perception.stones import HSVHandleDetector, RawHSVHandleDetector

FEATURE_VERSION = "handle-context-hog-hsv-v1"
MODEL_FAMILY = "opencv-handle-forest-v1"
FEATURE_COUNT = 404
MAX_MODEL_BYTES = 16 * 1024**2


def context_features(rgb, center, *, flip=False):
    if rgb.ndim != 3 or rgb.shape[2] != 3 or min(rgb.shape[:2]) < 4 or rgb.dtype != np.uint8:
        raise ValueError("Handle context needs an RGB crop")
    point = np.asarray(center, dtype=float)
    if (
        point.shape != (2,)
        or not np.isfinite(point).all()
        or not (0 <= point[0] < rgb.shape[1] and 0 <= point[1] < rgb.shape[0])
    ):
        raise ValueError("Handle context center must lie inside the original crop")
    scale = 32 / max(12, rgb.shape[1] * 0.12)
    matrix = np.array(
        [[scale, 0, 16 - point[0] * scale], [0, scale, 16 - point[1] * scale]], dtype=np.float32
    )
    patch = cv2.warpAffine(rgb, matrix, (32, 32), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT_101)
    if flip:
        patch = np.ascontiguousarray(patch[:, ::-1])
    gray = cv2.cvtColor(patch, cv2.COLOR_RGB2GRAY)
    hog = cv2.HOGDescriptor((32, 32), (16, 16), (8, 8), (8, 8), 9).compute(gray).reshape(-1)
    hsv = cv2.cvtColor(patch, cv2.COLOR_RGB2HSV)
    histogram = cv2.calcHist([hsv], [0, 1], None, [8, 4], [0, 180, 0, 256]).reshape(-1)
    histogram /= max(1, histogram.sum())
    colors = cv2.resize(patch, (4, 4), interpolation=cv2.INTER_AREA).astype(np.float32).reshape(-1) / 255
    features = np.concatenate([hog, histogram, colors]).astype(np.float32)
    if features.shape != (FEATURE_COUNT,) or not np.isfinite(features).all():
        raise ValueError("Handle feature descriptor does not match its version")
    return features


def classifier_votes(model, features):
    votes = np.asarray(model.getVotes(np.asarray(features, dtype=np.float32), 0))
    if votes.ndim != 2 or votes.shape[0] != len(features) + 1 or set(votes[0]) != {0, 1}:
        raise ValueError("Handle forest does not contain the required binary object classes")
    positive = int(np.flatnonzero(votes[0] == 1)[0])
    totals = votes[1:].sum(axis=1)
    if np.any(totals <= 0):
        raise ValueError("Handle forest returned no votes")
    return votes[1:, positive] / totals


class LearnedHandleDetector(HSVHandleDetector):
    def __init__(self, model, manifest):
        if (
            manifest.get("feature_version") != FEATURE_VERSION
            or manifest.get("raw_proposer_version") != RawHSVHandleDetector.version
            or not 0 < manifest.get("vote_threshold", 0) <= 1
            or not model.isTrained()
            or not model.isClassifier()
            or model.getVarCount() != FEATURE_COUNT
        ):
            raise ValueError("Learned handle model is incompatible with its feature manifest")
        self.model, self.manifest = model, manifest
        self.version = manifest["version"]
        self.threshold = manifest["vote_threshold"]

    def inspect(self, rgb):
        candidates = RawHSVHandleDetector.detect(self, rgb)
        if not candidates:
            return {"detections": [], "rejected_candidates": []}
        features = np.stack([context_features(rgb, d["center_px"]) for d in candidates])
        scores = classifier_votes(self.model, features)
        kept, rejected = [], []
        for candidate, score in zip(candidates, scores, strict=True):
            document = {
                **candidate,
                "classifier_vote": float(score),
                "classifier_version": self.version,
                "classifier_score_kind": "uncalibrated_fraction_of_tree_votes",
            }
            if score >= self.threshold:
                kept.append(document)
            else:
                rejected.append({**document, "rejection_reasons": ["learned_context_vote_below_threshold"]})
        return {"detections": kept, "rejected_candidates": rejected}


def load_learned_detector(root, manifest):
    artifact = manifest["model"]
    path = (root / artifact["path"]).resolve()
    if (
        not path.is_relative_to((root / "models/trained-handles").resolve())
        or not path.is_file()
        or not 0 < path.stat().st_size == artifact["bytes"] <= MAX_MODEL_BYTES
        or hashlib.sha256(path.read_bytes()).hexdigest() != artifact["sha256"]
    ):
        raise ValueError("Learned handle weights are missing, corrupt or outside their data directory")
    try:
        model = cv2.ml.RTrees_load(str(path))
    except cv2.error as exc:
        raise ValueError("Learned handle weights cannot be loaded") from exc
    return LearnedHandleDetector(model, manifest)
