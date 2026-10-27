import hashlib
from uuid import NAMESPACE_URL, uuid5

import cv2
import numpy as np
from PIL import Image
from sqlalchemy import select

from hogline.db import Evidence, FrameObservation
from hogline.perception.stones import VERSION, HSVHandleDetector, StoneDetector


def analyze_frame(session, evidence, root, detector: StoneDetector | None = None):
    detector = detector or HSVHandleDetector()
    observation_id = str(uuid5(NAMESPACE_URL, f"{evidence.id}:{detector.version}"))
    existing = session.get(FrameObservation, observation_id)
    if existing is not None:
        return existing.document
    frame, views = evidence.document, {}
    for view in ("house_top", "house_bottom"):
        asset = frame["crops"][view]
        path = root / asset["path"]
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != asset["sha256"]:
            raise ValueError("Cannot analyze missing/corrupt evidence; reacquire that frame first")
        image = np.asarray(Image.open(path).convert("RGB"))
        detections = detector.detect(image)
        for index, detection in enumerate(detections):
            detection["id"] = f"{observation_id}:{view}:{index}"
            detection["evidence_id"] = evidence.id
            detection["youtube_url"] = frame.get("youtube_url")
        views[view] = {"width": image.shape[1], "height": image.shape[0], "detections": detections}
    document = {
        "id": observation_id,
        "evidence_id": evidence.id,
        "model_version": detector.version,
        "opencv_version": cv2.__version__,
        "source_id": evidence.source_id,
        "pts": frame["pts"],
        "time_base_num": frame["time_base_num"],
        "time_base_den": frame["time_base_den"],
        "youtube_url": frame.get("youtube_url"),
        "views": views,
        "acceptance": "proposed",
        "calibration_status": "unavailable",
        "score": None,
        "score_missing_reason": "uncalibrated_handles_and_unknown_house_phase",
    }
    session.add(
        FrameObservation(
            id=observation_id, evidence_id=evidence.id, model_version=detector.version, document=document
        )
    )
    return document


def analyze_cached(db):
    """Reprocess retained frames with no video acquisition or interpretation edits."""
    with db.session() as session:
        return [
            analyze_frame(session, frame, db.settings.data_dir) for frame in session.scalars(select(Evidence))
        ]


def observations(db):
    with db.session() as session:
        return [
            r.document
            for r in session.scalars(
                select(FrameObservation).where(FrameObservation.model_version == VERSION)
            )
        ]
