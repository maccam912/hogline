from uuid import NAMESPACE_URL, uuid5

import cv2
import numpy as np
from sqlalchemy import select

from hogline.db import Evidence, FrameObservation
from hogline.media.frames import read_image
from hogline.perception.board_locator import analyze_board_location
from hogline.perception.house import house_ring
from hogline.perception.stones import VERSION, HSVHandleDetector, StoneDetector


def analyze_frame(session, evidence, root, detector: StoneDetector | None = None):
    if evidence.document.get("acquisition_tier") == "coarse":
        raise ValueError(
            "Coarse scans only support activity; acquire a detailed evidence frame for perception"
        )
    analyze_board_location(session, evidence, root)
    detector = detector or HSVHandleDetector()
    observation_id = str(uuid5(NAMESPACE_URL, f"{evidence.id}:{detector.version}"))
    existing = session.get(FrameObservation, observation_id)
    if existing is not None:
        return existing.document
    frame, views = evidence.document, {}
    for view in ("house_top", "house_bottom"):
        asset = frame["crops"][view]
        image = np.asarray(read_image(root, asset))
        analysis = (
            detector.inspect(image)
            if isinstance(detector, HSVHandleDetector)
            else {"detections": detector.detect(image), "rejected_candidates": []}
        )
        detections = analysis["detections"]
        for index, detection in enumerate(detections):
            detection["id"] = f"{observation_id}:{view}:{index}"
            detection["evidence_id"] = evidence.id
            detection["youtube_url"] = frame.get("youtube_url")
        views[view] = {
            "width": image.shape[1],
            "height": image.shape[0],
            "detections": detections,
            "rejected_candidates": analysis["rejected_candidates"],
            "house_ring": house_ring(image),
        }
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


def prepare_frame_observations(db, documents, tick=lambda: None):
    """Compute frame analysis in a read snapshot; publication holds the writer only for inserts."""
    prepared = []
    with db.snapshot_session() as session, session.no_autoflush:
        for doc in documents:
            tick()
            evidence = session.get(Evidence, doc["id"]) or Evidence(
                id=doc["id"], source_id=doc["source_id"], evidence_key=doc["evidence_key"], document=doc
            )
            analyze_frame(session, evidence, db.settings.data_dir)
            for row in list(session.new):
                if not isinstance(row, FrameObservation):
                    raise ValueError("Unexpected write during frame preparation")
                prepared.append(row)
                session.expunge(row)
            tick()
    return prepared


def publish_frame_observations(session, prepared):
    for row in prepared:
        if session.get(FrameObservation, row.id) is None:
            session.add(row)


def analyze_cached(db):
    """Reprocess retained frames with no video acquisition or interpretation edits."""
    with db.session() as session:
        return [
            analyze_frame(session, frame, db.settings.data_dir)
            for frame in session.scalars(select(Evidence))
            if frame.document.get("acquisition_tier") != "coarse"
        ]


def observations(db):
    with db.session() as session:
        return observations_in_session(session)


def observations_in_session(session, source_id=None):
    query = select(FrameObservation).where(FrameObservation.model_version == VERSION)
    if source_id is not None:
        query = query.join(Evidence, FrameObservation.evidence_id == Evidence.id).where(
            Evidence.source_id == source_id
        )
    return [r.document for r in session.scalars(query)]
