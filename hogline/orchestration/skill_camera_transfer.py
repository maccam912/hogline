"""Propose camera-map reuse from retained images, requiring a fresh landmark review."""

import io
from uuid import uuid4

from PIL import Image
from pydantic import Field

from hogline.adapters.sources import digest
from hogline.db import GameRow
from hogline.domain.models import Model
from hogline.domain.skill_calibration import CalibrationTransfer, CameraCalibration
from hogline.media.frames import read_image_bytes
from hogline.orchestration.skill_calibration import (
    calibration_result,
    checked_calibration,
    checked_motion,
    reference_image,
)
from hogline.perception.camera_registration import register_camera_images
from hogline.perception.landmark_calibration import inside_support, map_points


class CameraComparisonRequest(Model):
    reference_motion_id: str
    reference_sample_index: int = Field(default=-1, ge=-1, le=159)


class CalibrationTransferRequest(Model):
    parent_id: str
    parent_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    target_motion_id: str


def compare_in_session(session, game, root, reference_id, target_id, sample_index=-1):
    original = checked_motion(session, game, reference_id)
    target = checked_motion(session, game, target_id)
    a, b = original.document, target.document
    if a["source_id"] != b["source_id"] or a["request"]["view"] != b["request"]["view"]:
        raise ValueError("Camera reuse requires the same recording and camera view")
    if a.get("profile_sha256") != b.get("profile_sha256"):
        raise ValueError("Camera profile changed between clips")
    asset, sample, name = reference_image(a, sample_index)
    other_asset, other_sample, _ = reference_image(b, -1)
    for key in ("source_width", "source_height", "pixel_crop_bounds", "crop_width", "crop_height"):
        if sample.get(key) != other_sample.get(key):
            raise ValueError("Native camera crop geometry changed between clips")
    with Image.open(io.BytesIO(read_image_bytes(root, asset))) as reference:
        with Image.open(io.BytesIO(read_image_bytes(root, other_asset))) as image:
            result = register_camera_images(reference, image)
    return {
        **result,
        "reference_motion_id": reference_id,
        "target_motion_id": target_id,
        "reference_sha256": original.sha256,
        "target_sha256": target.sha256,
        "reference_image_sha256": asset["sha256"],
        "target_image_sha256": other_asset["sha256"],
        "reference_image_url": f"/api/skill-motion/{reference_id}/images/{name}.png",
        "target_image_url": f"/api/skill-motion/{target_id}/images/reference.png",
    }, target


def compare_cameras(db, game_id, motion_id, request):
    with db.snapshot_session() as session:
        result, _ = compare_in_session(
            session,
            session.get(GameRow, game_id),
            db.settings.data_dir,
            request.reference_motion_id,
            motion_id,
            request.reference_sample_index,
        )
        return result


def transfer_calibration(db, game_id, request):
    from hogline.domain.skills import SkillDocument, read_skills

    with db.snapshot_session() as session:
        review = read_skills(session, game_id)
        if review["stale"]:
            raise ValueError("Review changed game evidence before reusing a camera map")
        document = SkillDocument.model_validate(review["document"])
        parent = next((c for c in document.calibrations if c.id == request.parent_id), None)
        if parent is None or parent.acceptance != "confirmed" or parent.transfer:
            raise ValueError("Choose an original confirmed camera map; chained transfers are unsupported")
        if digest(parent.model_dump()) != request.parent_sha256:
            raise ValueError("Original camera map changed; reload before reusing it")
        if parent.motion_id == request.target_motion_id:
            raise ValueError("Choose another motion clip for camera reuse")
        game = session.get(GameRow, game_id)
        original = checked_calibration(session, game, parent, db.settings.data_dir, document.calibrations)
        if original["fit"]["status"] != "validated_geometry":
            raise ValueError("Original camera map has not passed independent landmark checks")
        registration, target = compare_in_session(
            session,
            game,
            db.settings.data_dir,
            parent.motion_id,
            request.target_motion_id,
            parent.reference_sample_index,
        )
        if registration["status"] != "alignment_proposal":
            return {"document": None, "registration": registration}
        pixels = map_points([p.pixel for p in parent.landmarks], registration["matrix"])
        size = (
            target.document["reference_sample"]["crop_width"],
            target.document["reference_sample"]["crop_height"],
        )
        if any(not 0 <= x < size[0] or not 0 <= y < size[1] for x, y in pixels):
            return {
                "document": None,
                "registration": {**registration, "reason": "mapped_landmarks_outside_target_frame"},
            }
        # Predictions outside the feature hull are explicitly marked; copied
        # check points never count as independent evidence until reviewed.
        supported = inside_support(
            [p.pixel for p in parent.landmarks], registration["feature_support_polygon_px"]
        )
        registration["landmarks_outside_feature_support"] = [
            p.label for p, ok in zip(parent.landmarks, supported, strict=True) if not ok
        ]
        draft = CameraCalibration.model_validate(
            {
                **parent.model_dump(),
                "id": str(uuid4()),
                "motion_id": target.id,
                "motion_sha256": target.sha256,
                "image_sha256": target.document["reference_asset"]["sha256"],
                "reference_sample_index": -1,
                "start_seconds": min(
                    target.document["request"]["start_seconds"],
                    target.document["reference_sample"]["source_seconds"],
                ),
                "end_seconds": target.document["request"]["end_seconds"],
                "acceptance": "proposed",
                "landmarks": [
                    {**p.model_dump(), "pixel": pixel.tolist()}
                    for p, pixel in zip(parent.landmarks, pixels, strict=True)
                ],
                "transfer": CalibrationTransfer(
                    parent_id=parent.id, parent_sha256=request.parent_sha256
                ).model_dump(),
                "basis": (
                    "Landmark predictions transferred from a reviewed camera map. Independently locate the "
                    "landmarks, especially check points, in this target frame before confirming."
                ),
                "evidence": [
                    f"skill-motion:{target.id}:reference",
                    f"camera-calibration:{parent.id}:{request.parent_sha256}",
                ],
            }
        )
        checked = checked_calibration(session, game, draft, db.settings.data_dir, document.calibrations)
        return {
            "document": draft.model_dump(),
            "registration": registration,
            **calibration_result(draft, checked),
        }
