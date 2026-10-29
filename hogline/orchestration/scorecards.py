"""Durable, bounded scoreboard/ice inspection of retained full-resolution frames."""

import hashlib
import time
from uuid import NAMESPACE_URL, uuid5

from PIL import Image
from sqlalchemy import select

from hogline.adapters.sources import digest
from hogline.config import profile
from hogline.db import Evidence, FrameObservation, Job, ProcessingBatch, now
from hogline.domain.catalog import source_or_error
from hogline.orchestration.plans import (
    check_execution,
    check_execution_in_session,
    finish_execution,
    publish_batch,
)
from hogline.perception.board_header import register_header
from hogline.perception.board_locator import locate_board
from hogline.perception.broadcast import broadcast_catalog_in_session
from hogline.perception.house_score import normalized_point
from hogline.perception.layout import reference_image
from hogline.perception.ocr import TesseractOCR
from hogline.perception.pipeline import VERSION as DETECTOR_VERSION
from hogline.perception.scoreboard import read_board
from hogline.perception.scorecard import VERSION, existing_template, fixed_header_cells, read_middle_row

KIND = "scorecard_read"


def choose_reference(observations, frames, layout):
    candidates = []
    for observation in observations:
        template = existing_template(observation, layout)
        frame = frames.get(observation["evidence_id"])
        if template is None or frame is None:
            continue
        board = observation["scoreboard"]
        quality = (
            frame.document["image"]["width"],
            -len(board.get("unreadable_slots", [])),
            board.get("header_registration", {}).get("header_correlation", 0.9),
            -observation["source_seconds"],
        )
        candidates.append((quality, frame, template))
    if not candidates:
        raise ValueError("No checked local scoreboard header reference is available for this recording")
    _, frame, template = max(candidates, key=lambda c: c[0])
    return {
        "evidence_id": frame.id,
        "image": frame.document["image"],
        "source_metadata_sha256": frame.document.get("source_metadata_sha256"),
        "template": template,
    }


def create_scorecard_batch(db, source_id, *, frames_per_job=12):
    if not 1 <= frames_per_job <= 24:
        raise ValueError("Scorecard jobs support one to 24 retained frames")
    layout = profile()
    with db.snapshot_session(write=True) as session:
        source = source_or_error(session, source_id)
        for owner in session.scalars(
            select(ProcessingBatch).where(ProcessingBatch.state.in_(["active", "paused"]))
        ):
            if owner.document.get("kind") == KIND and owner.document["request"]["source_ids"] == [source_id]:
                from hogline.orchestration.plans import batch_view

                return batch_view(session, owner)
        frames = {
            e.id: e
            for e in session.scalars(select(Evidence).where(Evidence.source_id == source_id))
            if e.document.get("acquisition_tier") != "coarse"
            and e.document.get("source_metadata_sha256") == source["metadata_sha256"]
        }
        observations = [
            o
            for o in broadcast_catalog_in_session(session, db.settings.data_dir)["observations"]
            if o["source_id"] == source_id and o["evidence_id"] in frames
        ]
        reference = choose_reference(observations, frames, layout)
        fixed_template = layout["broadcast"].get("automatic_board_header", {}).get("reference")
        _, fixed_manifest = (
            reference_image(session, db.settings.data_dir, fixed_template) if fixed_template else (None, {})
        )
        fixed_frame = (
            session.get(Evidence, fixed_manifest["evidence_id"])
            if fixed_manifest.get("evidence_id")
            else None
        )
        fixed = (
            {
                "evidence_id": fixed_frame.id,
                "image": fixed_frame.document["image"],
                "template": fixed_template,
            }
            if fixed_frame
            else None
        )
        version = f"{VERSION}:{digest({'profile': layout, 'reference': reference, 'fixed': fixed})[:16]}"
        parents = {o["evidence_id"]: o for o in observations}
        parent_hashes = {
            o["id"]: digest(session.get(FrameObservation, o["id"]).document) for o in observations
        }
        ordered = sorted(frames.values(), key=lambda f: (f.document["source_seconds"], f.id))
        units = []
        for index in range(0, len(ordered), frames_per_job):
            group = ordered[index : index + frames_per_job]
            entries = [
                {
                    "id": f.id,
                    "sha256": digest(f.document),
                    "parent_id": parents[f.id]["id"] if f.id in parents else None,
                    "parent_sha256": parent_hashes[parents[f.id]["id"]] if f.id in parents else None,
                }
                for f in group
            ]
            payload = {
                "kind": KIND,
                "stage_version": VERSION,
                "model_version": version,
                "source_hash": source["metadata_sha256"],
                "profile": layout,
                "reference": reference,
                "fixed_header_reference": fixed,
                "detector_version": DETECTOR_VERSION,
                "frames": entries,
                "request": {
                    "source_id": source_id,
                    "start_seconds": group[0].document["source_seconds"],
                    "end_seconds": group[-1].document["source_seconds"] + 0.001,
                    "max_worker_seconds": 300,
                    "max_evidence_mb": 64,
                },
            }
            units.append({"payload": payload, "disposition": "new"})
        plan = {
            "kind": KIND,
            "parallelism": 3,
            "phase": "scorecards",
            "coverage_kind": "retained_native_frames; continuous_ice_pass_pending",
            "request": {
                "source_ids": [source_id],
                "start_seconds": 0,
                "end_seconds": source["duration_seconds"],
                "max_worker_seconds": 86400,
                "max_image_growth_mb": 64,
            },
            "frames": len(ordered),
            "units": units,
            "created_at": now(),
            "reference": reference,
        }
        plan["sha256"] = digest(plan)
        return publish_batch(session, plan)


def checked_image(root, asset):
    path = (root / asset["path"]).resolve()
    if (
        not path.is_relative_to((root / "evidence").resolve())
        or not path.is_file()
        or hashlib.sha256(path.read_bytes()).hexdigest() != asset["sha256"]
    ):
        raise ValueError("Scorecard source image is missing, corrupt, or outside the evidence archive")
    with Image.open(path) as image:
        return image.convert("RGB")


def ice_measurements(detector):
    views = {}
    for name, view in detector.get("views", {}).items():
        ring = view["house_ring"]
        handles = []
        if ring.get("status") == "candidate":
            for handle in view["detections"]:
                point = normalized_point(handle["center_px"], ring)
                if float(point @ point) <= 1.16**2:
                    handles.append(
                        {
                            "color": handle["color"],
                            "center_px": handle["center_px"],
                            "normalized_point": point.tolist(),
                            "bbox_px": handle["bbox_px"],
                        }
                    )
        views[name] = {
            "ring": ring,
            "handles": handles,
            "red_count": sum(h["color"] == "red" for h in handles),
            "yellow_count": sum(h["color"] == "yellow" for h in handles),
            "zero_meaning": "no_handles_detected; not_proof_of_empty_house",
        }
    return views


def read_frame(image, frame, parent, job, reference_image, reader, fixed_reference=None):
    layout, board = job["profile"], (parent or {}).get("scoreboard", {})
    template = existing_template(parent, layout) if parent else None
    if template is None:
        location = locate_board(image, layout["views"]["perspective_left"])
        board = {"status": "unconfigured", "cards": [], "missing_reason": location["missing_reason"]}
        candidate = location.get("selected")
        fixed_geometry = False
        if candidate is None:
            known = locate_board(reference_image, layout["views"]["perspective_left"])
            candidate, fixed_geometry = known.get("selected"), True
        if candidate:
            registration = register_header(
                image,
                candidate,
                reference_image,
                job["reference"]["template"],
                layout["views"]["perspective_left"],
            )
            for tail_start in (8, 11):
                if registration["status"] == "proposed":
                    break
                registration = register_header(
                    image,
                    candidate,
                    reference_image,
                    job["reference"]["template"],
                    layout["views"]["perspective_left"],
                    fixed_tail_start=tail_start,
                )
            registration["live_color_bands_occluded"] = fixed_geometry
            if registration["status"] == "proposed":
                template = registration["template"]
                board = {
                    "status": "observed",
                    **read_board(image, template, reader),
                    "effective_bounds": template["bounds"],
                    "header_registration": registration,
                }
            else:
                board["missing_reason"] = registration["missing_reason"]
    if template:
        middle = read_middle_row(image, template, reader, fixed_reference)
        if fixed_reference is None:
            middle["unverified_blank_candidates"] = middle["blank_end_cards"]
            middle["blank_end_cards"] = []
        board = {**board, **middle}
    return board


def execute_scorecard_read(db, job):
    from hogline.orchestration.worker import update_owned

    started, token = time.monotonic(), job["lease_token"]
    if job["stage_version"] != VERSION or job["detector_version"] != DETECTOR_VERSION:
        raise ValueError("Scorecard inspection methods changed; start current work")
    check_execution(db, token, 0)
    reference = checked_image(db.settings.data_dir, job["reference"]["image"])
    fixed = job.get("fixed_header_reference")
    fixed_reference = (
        fixed_header_cells(checked_image(db.settings.data_dir, fixed["image"]), fixed["template"])
        if fixed
        else None
    )
    reader = TesseractOCR(deadline=started + job["request"]["max_worker_seconds"])
    if not reader.available:
        raise ValueError("Install Tesseract for scoreboard inspection")
    completed = []
    for index, entry in enumerate(job["frames"]):
        check_execution(db, token, time.monotonic() - started)
        identifier = str(uuid5(NAMESPACE_URL, f"{entry['id']}:{job['model_version']}"))
        with db.session() as session:
            source = source_or_error(session, job["request"]["source_id"])
            frame = session.get(Evidence, entry["id"])
            parent = session.get(FrameObservation, entry["parent_id"]) if entry["parent_id"] else None
            existing = session.get(FrameObservation, identifier)
            if (
                source["metadata_sha256"] != job["source_hash"]
                or frame is None
                or digest(frame.document) != entry["sha256"]
            ):
                raise ValueError("Scorecard source/evidence changed after scheduling")
            if entry["parent_id"] and (
                parent is None
                or parent.evidence_id != frame.id
                or digest(parent.document) != entry["parent_sha256"]
            ):
                raise ValueError("Scorecard parent reader observation changed after scheduling")
            if parent:
                parent_doc = dict(parent.document)
            else:
                parent_doc = None
            detector = session.get(
                FrameObservation, str(uuid5(NAMESPACE_URL, f"{frame.id}:{DETECTOR_VERSION}"))
            )
        if existing is None:
            image = checked_image(db.settings.data_dir, frame.document["image"])
            board = read_frame(image, frame, parent_doc, job, reference, reader, fixed_reference)
            document = {
                "id": identifier,
                "kind": "scorecard_board_observation",
                "model_version": job["model_version"],
                "source_id": frame.source_id,
                "source_metadata_sha256": job["source_hash"],
                "evidence_id": frame.id,
                "source_image": frame.document["image"],
                "profile_sha256": digest(job["profile"]),
                **{
                    k: frame.document[k]
                    for k in ("source_seconds", "pts", "time_base_num", "time_base_den", "youtube_url")
                },
                "scoreboard": board,
                "parent_observation_id": parent.id if parent else None,
                "parent_observation_sha256": digest(parent_doc) if parent_doc else None,
                "reference": job["reference"],
                "fixed_header_reference": fixed,
                "ice_views": ice_measurements(detector.document) if detector else {},
                "detector_observation_id": detector.id if detector else None,
                "detector_observation_sha256": digest(detector.document) if detector else None,
            }
            document["sha256"] = digest(document)
            with db.snapshot_session(write=True) as session:
                stopped = check_execution_in_session(session, token, time.monotonic() - started)
                if stopped:
                    from hogline.orchestration.plans import WorkStopped

                    raise WorkStopped(stopped)
                if session.get(FrameObservation, identifier) is None:
                    session.add(
                        FrameObservation(
                            id=identifier,
                            evidence_id=frame.id,
                            model_version=job["model_version"],
                            document=document,
                        )
                    )
        elif digest({k: v for k, v in existing.document.items() if k != "sha256"}) != existing.document.get(
            "sha256"
        ):
            raise ValueError("Cached scorecard observation checksum mismatch")
        completed.append(identifier)
        update_owned(
            db, job["id"], token, completed_frames=index + 1, progress=(index + 1) / len(job["frames"])
        )
    with db.snapshot_session(write=True) as session:
        stopped = check_execution_in_session(session, token, time.monotonic() - started)
        if stopped:
            from hogline.orchestration.plans import WorkStopped

            raise WorkStopped(stopped)
        row = session.get(Job, job["id"])
        row.state, row.lease_until = "succeeded", None
        row.document = {**row.document, "finished_at": now(), "observation_ids": completed}
        finish_execution(session, token, "succeeded", time.monotonic() - started)
    return {"observation_ids": completed}
