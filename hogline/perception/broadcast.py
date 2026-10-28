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
from hogline.perception.layout import board_alignment, reference_image
from hogline.perception.ocr import TesseractOCR, normalized_crop
from hogline.perception.overlay_clock import read_clock
from hogline.perception.scoreboard import persistent_board_claims, read_board

VERSION = "traditional-board-clock-v7"


def broadcast_version(layout):
    return f"{VERSION}:{digest(layout.get('broadcast', {}))[:16]}"


def template_applies(template, source, seconds):
    """Explicit source/time scope; sheet identity alone cannot select a layout."""
    key = f"{source['platform']}:{source['platform_id']}"
    return any(
        scope["source_key"] == key and scope["start_seconds"] <= seconds < scope["end_seconds"]
        for scope in template.get("source_intervals", [])
    )


def analyze_broadcast_frame(session, frame, root, layout, reader, budget):
    if frame.document.get("acquisition_tier") == "coarse":
        return {"evidence_id": frame.id, "status": "skipped", "missing_reason": "coarse_scan_only"}
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
    clock = parse_overlay("", "", layout["timezone"])
    crops = {}
    if clock_config:
        date_image = normalized_crop(image, clock_config["date_bounds"])
        time_image = normalized_crop(image, clock_config["time_bounds"])
        clock, tight_date = read_clock(date_image, time_image, reader, layout["timezone"])
        crops["clock_date"] = save_image(date_image, root, budget)
        crops["clock_date_tight"] = save_image(tight_date, root, budget)
        crops["clock_time"] = save_image(time_image, root, budget)
    clock["template_status"] = clock_config.get("status", "unconfigured")
    board_config = config.get("scoreboards", {}).get(str(source.get("sheet")))
    board = {
        "status": "unconfigured",
        "missing_reason": "sheet_scoreboard_template_not_configured",
        "cards": [],
        "acceptance": "unknown",
    }
    if board_config and template_applies(board_config, source, frame.document["source_seconds"]):
        reference, reference_manifest = reference_image(session, root, board_config)
        alignment = (
            board_alignment(image, reference, board_config) if reference is not None else reference_manifest
        )
        board["layout_alignment"] = {**alignment, "reference": reference_manifest}
        if alignment["status"] in ("compatible_seed", "translated_seed"):
            effective_config = {**board_config, "bounds": alignment["effective_bounds"]}
            board = {**board, "status": "observed", **read_board(image, effective_config, reader)}
            board["template_source_intervals"] = board_config["source_intervals"]
            board["effective_bounds"] = effective_config["bounds"]
            board["missing_reason"] = None
            crops["scoreboard"] = {
                **save_image(normalized_crop(image, effective_config["bounds"]), root, budget),
                "normalized_bounds": effective_config["bounds"],
            }
            if alignment["status"] == "translated_seed":
                crops["scoreboard_seed"] = save_image(
                    normalized_crop(image, board_config["bounds"]), root, budget
                )
        else:
            board["missing_reason"] = alignment.get("missing_reason", "scoreboard_layout_drift_or_occlusion")
            crops["scoreboard_candidate"] = save_image(
                normalized_crop(image, board_config["bounds"]), root, budget
            )
    elif board_config:
        board["missing_reason"] = "scoreboard_template_not_applicable_to_source_time"
    doc = {
        "id": identifier,
        "kind": "broadcast_observation",
        "evidence_id": frame.id,
        "source_id": frame.source_id,
        "source_metadata_sha256": frame.document.get("source_metadata_sha256"),
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
    with db.session() as session:
        return broadcast_catalog_in_session(session)


def broadcast_catalog_in_session(session):
    version = broadcast_version(profile())
    observations = [
        r.document
        for r in session.scalars(select(FrameObservation).where(FrameObservation.model_version == version))
    ]
    return {
        "observations": observations,
        "clock_hypotheses": fit_clock_hypotheses(observations),
        "score_proposals": persistent_board_claims(observations),
        "version": version,
    }
