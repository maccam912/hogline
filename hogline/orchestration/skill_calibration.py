"""Bind reviewed camera landmarks to immutable retained motion evidence."""

import io
from uuid import uuid4

from PIL import Image

from hogline.adapters.sources import digest
from hogline.db import GameRow, SkillMotion, SkillReview, SkillSequence, Source
from hogline.domain.skill_calibration import CameraCalibration
from hogline.domain.skill_scope import compatible_game_scope
from hogline.media.frames import read_image_bytes
from hogline.perception.landmark_calibration import fit_calibration, project_track
from hogline.perception.shot_aim import measure_aim


def checked_motion(session, game, motion_id):
    if game is None:
        raise ValueError("Game not found")
    row = session.get(SkillMotion, motion_id)
    if row is None or digest(row.document) != row.sha256:
        raise ValueError("Calibration motion is missing or changed")
    doc = row.document
    lineage = doc.get("retained_detection_parent")
    if lineage:
        parent = session.get(SkillMotion, lineage["motion_id"])
        if (
            parent is None
            or parent.game_id != game.id
            or parent.sha256 != lineage["sha256"]
            or digest(parent.document) != lineage["sha256"]
            or parent.document.get("retained_detection_parent")
        ):
            raise ValueError("Retained motion detection evidence changed")
    source = session.get(Source, doc["source_id"])
    if (
        row.game_id != game.id
        or game.document.get("retired_into")
        or not compatible_game_scope(session, game, doc["game_revision"])
        or source is None
        or doc["source_metadata_sha256"] != source.document["metadata_sha256"]
    ):
        raise ValueError("Calibration motion no longer matches this game and source")
    anchor = doc.get("delivery_anchor")
    if anchor:
        sequence = session.get(SkillSequence, anchor["sequence_id"])
        if (
            sequence is None
            or sequence.sha256 != anchor["sha256"]
            or digest(sequence.document) != anchor["sha256"]
        ):
            raise ValueError("Calibration motion delivery evidence changed")
    return row


def reference_image(motion, sample_index):
    if sample_index == -1:
        return motion["reference_asset"], motion["reference_sample"], "reference"
    retained = [f for f in motion["retained_samples"] if f["sample_index"] == sample_index]
    if len(retained) != 1 or not 0 <= sample_index < len(motion["samples"]):
        raise ValueError("Calibration needs an original retained frame")
    return retained[0]["asset"], motion["samples"][sample_index], str(sample_index)


def checked_calibration(session, game, calibration, root, calibrations=None):
    row = checked_motion(session, game, calibration.motion_id)
    doc = row.document
    if (
        calibration.motion_sha256 != row.sha256
        or calibration.source_id != doc["source_id"]
        or calibration.view != doc["request"]["view"]
    ):
        raise ValueError("Calibration is bound to a different motion, source or camera")
    if not any(
        span["source_id"] == calibration.source_id
        and span["start_seconds"]
        <= calibration.start_seconds
        < calibration.end_seconds
        <= span["end_seconds"]
        for span in game.document["spans"]
    ):
        raise ValueError("Calibration interval must lie within a game source span")
    asset, sample, name = reference_image(doc, calibration.reference_sample_index)
    if calibration.image_sha256 != asset["sha256"]:
        raise ValueError("Calibration reference image changed")
    if not calibration.start_seconds <= sample["source_seconds"] <= calibration.end_seconds:
        raise ValueError("Calibration reference must lie inside its validity interval")
    with Image.open(io.BytesIO(read_image_bytes(root, asset))) as image:
        size = image.size
    if size != (sample["crop_width"], sample["crop_height"]):
        raise ValueError("Calibration reference geometry changed")
    if calibration.transfer:
        if calibrations is None:
            review = session.get(SkillReview, game.id)
            if review is None or digest(review.document) != review.sha256:
                raise ValueError("Calibration transfer needs the original saved camera map")
            calibrations = [
                CameraCalibration.model_validate(c) for c in review.document.get("calibrations", [])
            ]
        parent = next((c for c in calibrations if c.id == calibration.transfer.parent_id), None)
        if (
            parent is None
            or parent.id == calibration.id
            or parent.transfer
            or parent.acceptance != "confirmed"
            or digest(parent.model_dump()) != calibration.transfer.parent_sha256
        ):
            raise ValueError("Calibration transfer parent is absent, changed or unconfirmed")
        if (parent.source_id, parent.view, parent.coordinate_system, parent.dimension_basis) != (
            calibration.source_id,
            calibration.view,
            calibration.coordinate_system,
            calibration.dimension_basis,
        ):
            raise ValueError(
                "Transferred calibration must retain the original source, camera and coordinates"
            )
        if {p.label: (p.plane_m, p.use) for p in calibration.landmarks} != {
            p.label: (p.plane_m, p.use) for p in parent.landmarks
        }:
            raise ValueError("Transferred calibration must retain the original landmark dimensions and uses")
        original = checked_calibration(session, game, parent, root)
        if original["fit"]["status"] != "validated_geometry":
            raise ValueError("Calibration transfer parent must have validated geometry")
        if doc.get("profile_sha256") != original["motion"].get("profile_sha256") or size != tuple(
            original["image_size"]
        ):
            raise ValueError("Transferred camera profile or image dimensions changed")
        _, original_sample, _ = reference_image(original["motion"], parent.reference_sample_index)
        if any(
            sample.get(key) != original_sample.get(key)
            for key in ("source_width", "source_height", "pixel_crop_bounds")
        ):
            raise ValueError("Transferred native camera crop geometry changed")
    return {
        "motion": doc,
        "fit": fit_calibration(calibration, size),
        "image_size": list(size),
        "image_url": f"/api/skill-motion/{row.id}/images/{name}.png",
    }


def calibration_preview(db, game_id, calibration):
    with db.snapshot_session() as session:
        game = session.get(GameRow, game_id)
        if game is None:
            raise ValueError("Game not found")
        checked = checked_calibration(session, game, calibration, db.settings.data_dir)
    return calibration_result(calibration, checked)


def calibration_result(calibration, checked):
    return {
        "id": calibration.id,
        "acceptance": calibration.acceptance,
        "calibration_sha256": digest(calibration.model_dump()),
        "cohort_id": calibration_cohort(calibration),
        "fit": checked["fit"],
        "image_size": checked["image_size"],
        "image_url": checked["image_url"],
        "measurements": [
            project_track(track, calibration, checked["fit"])
            for track in checked["motion"].get("image_motion_tracks", [])
        ],
    }


def calibration_cohort(calibration):
    return calibration.transfer.parent_id if calibration.transfer else calibration.id


def checked_shot_aim(shot, calibration_checks):
    observation = shot.aim
    if observation is None or observation.calibration_id not in calibration_checks:
        raise ValueError("Choose a saved camera calibration for this shot's aim")
    calibration, checked = calibration_checks[observation.calibration_id]
    if shot.source_id != calibration.source_id or not -1 <= observation.release_seconds - shot.seconds <= 10:
        raise ValueError("Aim evidence does not belong to this shot's source and delivery time")
    tracks = [t for t in checked["motion"].get("image_motion_tracks", []) if t["id"] == observation.track_id]
    if len(tracks) != 1:
        raise ValueError("Aim track is missing from the calibrated motion")
    if shot.side != "unknown" and tracks[0].get("color") not in (None, "unknown", shot.side):
        raise ValueError("Aim track color conflicts with the reviewed shot")
    return measure_aim(observation, calibration, checked["fit"], tracks[0], shot.turn)


def aim_preview(db, game_id, document, shot, stale=False):
    if stale:
        raise ValueError("Reload and review changed game evidence before measuring aim")
    if shot.aim is None:
        raise ValueError("Aim observations are required")
    calibration = next((c for c in document.calibrations if c.id == shot.aim.calibration_id), None)
    if calibration is None:
        raise ValueError("Camera calibration not found in this game's review")
    with db.snapshot_session() as session:
        game = session.get(GameRow, game_id)
        checked = checked_calibration(session, game, calibration, db.settings.data_dir, document.calibrations)
    result = checked_shot_aim(shot, {calibration.id: (calibration, checked)})
    draft = shot.model_copy(deep=True)
    draft.line_error_degrees = result["line_error_degrees"] if shot.aim.acceptance == "confirmed" else None
    draft.line_calibration = calibration_cohort(calibration)
    return {"measurement": result, "draft": draft.model_dump()}


def calibration_reports(db, game_id, calibrations, stale=False):
    result = []
    with db.snapshot_session() as session:
        game = session.get(GameRow, game_id)
        for calibration in calibrations:
            try:
                if stale:
                    raise ValueError("Saved skill review has stale evidence")
                checked = checked_calibration(session, game, calibration, db.settings.data_dir, calibrations)
                result.append(calibration_result(calibration, checked))
            except ValueError as exc:
                result.append(
                    {"id": calibration.id, "status": "unavailable", "reason": str(exc), "measurements": []}
                )
    return result


def calibration_draft(db, game_id, motion_id):
    with db.snapshot_session() as session:
        game = session.get(GameRow, game_id)
        if game is None:
            raise ValueError("Game not found")
        row = checked_motion(session, game, motion_id)
        doc = row.document
        start = min(doc["request"]["start_seconds"], doc["reference_sample"]["source_seconds"])
        draft = CameraCalibration(
            id=str(uuid4()),
            motion_id=row.id,
            motion_sha256=row.sha256,
            source_id=doc["source_id"],
            view=doc["request"]["view"],
            image_sha256=doc["reference_asset"]["sha256"],
            start_seconds=start,
            end_seconds=doc["request"]["end_seconds"],
            coordinate_system=(
                "First: fixed lateral meters; second: longitudinal meters from the chosen origin. "
                "Record the origin and positive directions here."
            ),
            basis="Landmarks and sheet dimensions need review.",
            evidence=[f"skill-motion:{row.id}:reference"],
        )
        checked = checked_calibration(session, game, draft, db.settings.data_dir)
    return {"document": draft.model_dump(), **calibration_result(draft, checked)}
