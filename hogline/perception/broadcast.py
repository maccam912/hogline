import hashlib
import time
from uuid import NAMESPACE_URL, uuid5

from PIL import Image
from sqlalchemy import select

from hogline.adapters.sources import digest
from hogline.config import profile
from hogline.db import Evidence, FrameObservation, Source
from hogline.media.clocks import fit_clock_hypotheses, parse_overlay
from hogline.media.frames import EvidenceBudget, save_image
from hogline.perception.ocr import TesseractOCR, normalized_crop
from hogline.perception.scoreboard import persistent_board_claims, read_board

VERSION = "traditional-board-clock-v3"


def broadcast_version(layout):
    return f"{VERSION}:{digest(layout.get('broadcast', {}))[:16]}"


def analyze_broadcast_frame(session, frame, root, layout, reader, budget):
    version = broadcast_version(layout)
    identifier = str(uuid5(NAMESPACE_URL, f"{frame.id}:{version}"))
    existing = session.get(FrameObservation, identifier)
    if existing is not None:
        return existing.document
    if not reader.available:
        return {"evidence_id": frame.id, "status": "unavailable", "missing_reason": "tesseract_not_installed"}
    asset = frame.document["image"]
    path = root / asset["path"]
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != asset["sha256"]:
        raise ValueError("Cannot OCR missing/corrupt evidence; restore this frame first")
    image = Image.open(path).convert("RGB")
    source = session.get(Source, frame.source_id).document
    config = layout.get("broadcast", {})
    clock_config = config.get("clock", {})
    date_read, time_read = {"text": ""}, {"text": ""}
    crops = {}
    if clock_config:
        date_image = normalized_crop(image, clock_config["date_bounds"])
        time_image = normalized_crop(image, clock_config["time_bounds"])
        date_read = reader.read(date_image, whitelist="0123456789/", psm=13)
        time_read = reader.read(time_image, whitelist="0123456789:APM", psm=7)
        crops["clock_date"] = save_image(date_image, root, budget)
        crops["clock_time"] = save_image(time_image, root, budget)
    clock = parse_overlay(date_read["text"], time_read["text"], layout["timezone"])
    clock["raw_date_ocr"], clock["raw_time_ocr"] = date_read, time_read
    clock["template_status"] = clock_config.get("status", "unconfigured")
    board_config = config.get("scoreboards", {}).get(str(source.get("sheet")))
    board = {
        "status": "unconfigured",
        "missing_reason": "sheet_scoreboard_template_not_configured",
        "cards": [],
        "acceptance": "unknown",
    }
    if board_config:
        board = {"status": "observed", **read_board(image, board_config, reader)}
        crops["scoreboard"] = save_image(normalized_crop(image, board_config["bounds"]), root, budget)
    doc = {
        "id": identifier,
        "kind": "broadcast_observation",
        "evidence_id": frame.id,
        "source_id": frame.source_id,
        "sheet": source.get("sheet"),
        "source_seconds": frame.document["source_seconds"],
        "pts": frame.document["pts"],
        "time_base_num": frame.document["time_base_num"],
        "time_base_den": frame.document["time_base_den"],
        "youtube_url": frame.document.get("youtube_url"),
        "model_version": version,
        "config": config,
        "clock": clock,
        "scoreboard": board,
        "crops": crops,
        "acceptance": "proposed",
    }
    session.add(FrameObservation(id=identifier, evidence_id=frame.id, model_version=version, document=doc))
    session.flush()
    return doc


def analyze_broadcast(db, evidence_ids=None, limit=120, max_seconds=120):
    layout = profile()
    budget = EvidenceBudget(db.settings.data_dir, 100, db.settings.max_cache_mb)
    reader = TesseractOCR(deadline=time.monotonic() + max_seconds)
    output = []
    with db.session() as session:
        query = select(Evidence)
        if evidence_ids is not None:
            query = query.where(Evidence.id.in_(evidence_ids))
        identifiers = list(session.scalars(query.limit(min(limit, 120))))
    # Publish each frame independently; an OCR failure cannot erase motion/stone outputs.
    for frame in identifiers:
        with db.session() as session:
            output.append(
                analyze_broadcast_frame(session, frame, db.settings.data_dir, layout, reader, budget)
            )
    return output


def broadcast_catalog(db):
    version = broadcast_version(profile())
    with db.session() as session:
        observations = [
            r.document
            for r in session.scalars(
                select(FrameObservation).where(FrameObservation.model_version == version)
            )
        ]
    return {
        "observations": observations,
        "clock_hypotheses": fit_clock_hypotheses(observations),
        "score_proposals": persistent_board_claims(observations),
        "version": version,
    }
