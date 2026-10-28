import hashlib
import math
import time
from uuid import NAMESPACE_URL, uuid5

from PIL import Image
from sqlalchemy import func, select

from hogline.adapters.sources import digest
from hogline.config import profile
from hogline.db import Evidence, FrameObservation, Source
from hogline.media.clocks import fit_clock_hypotheses, parse_overlay
from hogline.media.frames import EvidenceBudget, EvidenceLimitError, save_image
from hogline.perception.board_header import register_header
from hogline.perception.board_locator import locate_board
from hogline.perception.layout import board_alignment, reference_image
from hogline.perception.ocr import OCRBudgetReached, TesseractOCR, normalized_crop
from hogline.perception.overlay_clock import read_clock
from hogline.perception.scoreboard import persistent_board_claims, read_board
from hogline.reconstruction.board_events import board_timeline

VERSION = "traditional-board-clock-v10"


def broadcast_version(layout, reference_manifest=None):
    return (
        f"{VERSION}:{digest({'config': layout.get('broadcast', {}), 'reference': reference_manifest})[:16]}"
    )


def broadcast_context(session, root, layout):
    config = layout.get("broadcast", {})
    auto = config.get("automatic_board_header", {})
    template = auto.get("reference")
    reference, manifest = reference_image(session, root, template) if template else (None, None)
    return {
        "reference": reference,
        "reference_manifest": manifest,
        "reference_template": template,
        "version": broadcast_version(layout, manifest),
    }


def template_applies(template, source, seconds):
    """Explicit source/time scope; sheet identity alone cannot select a layout."""
    key = f"{source['platform']}:{source['platform_id']}"
    return any(
        scope["source_key"] == key and scope["start_seconds"] <= seconds < scope["end_seconds"]
        for scope in template.get("source_intervals", [])
    )


def analyze_broadcast_frame(session, frame, root, layout, reader, budget, *, context=None):
    if frame.document.get("acquisition_tier") == "coarse":
        return {"evidence_id": frame.id, "status": "skipped", "missing_reason": "coarse_scan_only"}
    context = context or broadcast_context(session, root, layout)
    version = context["version"]
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
    if board["status"] == "unconfigured" and config.get("automatic_board_header"):
        location = locate_board(image, frame.document["crops"]["perspective_left"]["normalized_bounds"])
        board["automatic_location"] = location
        if context["reference"] is None:
            board["missing_reason"] = "retained_header_reference_unavailable"
        elif location["status"] != "proposed":
            board["missing_reason"] = location["missing_reason"]
        else:
            registration = register_header(
                image,
                location["selected"],
                context["reference"],
                context["reference_template"],
                layout["views"]["perspective_left"],
            )
            board["header_registration"] = {
                **registration,
                "reference": context["reference_manifest"],
            }
            if registration["status"] == "proposed":
                effective = registration["template"]
                board = {**board, "status": "observed", **read_board(image, effective, reader)}
                board["effective_bounds"] = effective["bounds"]
                board["missing_reason"] = None
                crops["scoreboard"] = {
                    **save_image(normalized_crop(image, effective["bounds"]), root, budget),
                    "normalized_bounds": effective["bounds"],
                }
            else:
                board["missing_reason"] = registration["missing_reason"]
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
        "header_reference": context["reference_manifest"],
        "clock": clock,
        "scoreboard": board,
        "crops": crops,
        "acceptance": "proposed",
    }
    session.add(FrameObservation(id=identifier, evidence_id=frame.id, model_version=version, document=doc))
    session.flush()
    return doc


def analyze_broadcast(db, evidence_ids=None, limit=120, max_seconds=120):
    """Process only pending detailed frames; each bounded pass resumes the backlog."""
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 120:
        raise ValueError("OCR frame limit must be between 1 and 120")
    if not math.isfinite(max_seconds) or not 0 < max_seconds <= 120:
        raise ValueError("OCR time budget must be greater than zero and at most 120 seconds")
    layout = profile()
    with db.session() as session:
        context = broadcast_context(session, db.settings.data_dir, layout)
    version = context["version"]
    budget = EvidenceBudget(db.settings.data_dir, 100, db.settings.max_cache_mb)
    reader = TesseractOCR(deadline=time.monotonic() + max_seconds)
    if not reader.available:
        raise ValueError("Install Tesseract OCR to read retained clock/scoreboard frames locally")
    output = []
    processed = (
        select(FrameObservation.id)
        .where(FrameObservation.evidence_id == Evidence.id, FrameObservation.model_version == version)
        .exists()
    )
    query = select(Evidence).where(
        func.coalesce(Evidence.document["acquisition_tier"].as_string(), "evidence") != "coarse",
        ~processed,
    )
    if evidence_ids is not None:
        query = query.where(Evidence.id.in_(evidence_ids))
    ordered = query.order_by(Evidence.source_id, Evidence.document["source_seconds"].as_float(), Evidence.id)
    with db.session() as session:
        identifiers = list(session.scalars(ordered.limit(limit)))
    stopped_reason = None
    # Publish each frame independently; an OCR failure cannot erase motion/stone outputs.
    for frame in identifiers:
        try:
            if time.monotonic() >= reader.deadline:
                raise OCRBudgetReached("OCR time budget reached")
            with db.session() as session:
                observation = analyze_broadcast_frame(
                    session, frame, db.settings.data_dir, layout, reader, budget, context=context
                )
            output.append(observation)
        except OCRBudgetReached:
            stopped_reason = "worker_time_budget"
            break
        except EvidenceLimitError:
            stopped_reason = "evidence_storage_budget"
            break
    with db.session() as session:
        remaining = session.scalar(select(func.count()).select_from(query.subquery()))
    return {
        "observations": output,
        "processed_frames": len(output),
        "remaining_frames": remaining,
        "status": "partial" if remaining else "complete",
        "stopped_reason": stopped_reason or ("frame_limit" if remaining else None),
        "model_version": version,
    }


def broadcast_catalog(db):
    with db.session() as session:
        return broadcast_catalog_in_session(session, db.settings.data_dir)


def broadcast_catalog_in_session(session, root):
    context = broadcast_context(session, root, profile())
    version = context["version"]
    observations = [
        r.document
        for r in session.scalars(select(FrameObservation).where(FrameObservation.model_version == version))
    ]
    return {
        "observations": observations,
        "clock_hypotheses": fit_clock_hypotheses(observations),
        "score_proposals": persistent_board_claims(observations),
        "board_timeline": board_timeline(observations),
        "version": version,
    }
