"""Attach source-scoped perspective maps and paired-stone evidence to native rays."""

import copy
import json
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import numpy as np
from pydantic import Field
from sqlalchemy import select

from hogline.adapters.sources import digest
from hogline.config import profile
from hogline.db import Evidence, SkillDeliveryRay
from hogline.domain.catalog import source_or_error
from hogline.domain.models import Model
from hogline.domain.skill_estimates import estimate_report, latest_revision
from hogline.media.frames import EvidenceBudget, capture, observe, resolve_media
from hogline.perception.perspective_rays import VERSION, fit_perspective, paired_distance, project


class PerspectiveRayRequest(Model):
    candidate_id: str
    calibration_id: str = Field(default="wausau-sheet4-oct30-perspectives", pattern=r"^[a-z0-9-]{1,100}$")


def calibration_spec(calibration_id):
    path = Path(__file__).parents[1] / "media" / "references" / f"{calibration_id}.json"
    if not path.is_file():
        raise ValueError("No reviewed perspective landmarks for this calibration")
    return json.loads(path.read_text())


def prepare_perspectives(db, source, handle, spec):
    if source["id"] != spec["source_id"] or handle.get("sha256") != spec["media_sha256"]:
        raise ValueError("Perspective landmarks belong to another recording or media revision")
    layout = profile()
    frames, cameras = {}, {}
    # One parent plus four regenerable crop descriptors per selected frame.
    budget = EvidenceBudget(db.settings.data_dir, 32, db.settings.max_cache_mb)

    def retain(seconds):
        im, timing = capture(handle["uri"], max(0, seconds - 0.00001))
        if list(im.size) != spec["image_size"]:
            raise ValueError("Perspective calibration crop layout changed")
        actual = timing["pts"] * timing["time_base_num"] / timing["time_base_den"]
        if abs(actual - seconds) > 1 / 60 + 1e-6:
            raise ValueError("Paired-stone frame does not match the reviewed source instant")
        doc, _ = observe(im, timing, source, db.settings.data_dir, budget=budget)
        doc["id"] = str(uuid5(NAMESPACE_URL, f"perspective-ray-frame:{doc['evidence_key']}"))
        frames[doc["id"]] = doc
        return im, doc["id"], timing

    reference, reference_id, _ = retain(spec["reference_seconds"])
    variants = {}
    for view, seed in spec["cameras"].items():
        bounds = [round(v * s) for v, s in zip(layout["views"][view], list(reference.size) * 2, strict=True)]
        rgb = np.asarray(reference.crop(bounds))
        cameras[view] = {**fit_perspective(rgb, seed), "crop_bounds": bounds}
        variants[view] = []
        for boundary_shift in (0, 0.5, 1):
            for hog_shift in (-1, 0, 1):
                variants[view].append(
                    fit_perspective(rgb, seed, boundary_shift_px=boundary_shift, hog_shift_px=hog_shift)
                )
    pairs = []
    for pair in spec["pairs"]:
        _, frame_id, timing = retain(pair["source_seconds"])
        pairs.append({**pair, "frame_id": frame_id, "timing": timing})
    audit = paired_distance(cameras, pairs)
    sensitivity = []
    for pair in pairs:
        bounds = []
        for view in cameras:
            positions = [
                project([[pair[view][0], pair[view][1] + dy]], variant["pixel_to_local"])[0, 1]
                for variant in variants[view]
                for dy in (-1, 1)
            ]
            bounds.append([min(positions), max(positions)])
        sensitivity.extend([sum(b[0] for b in bounds) - 0.29, sum(b[1] for b in bounds) - 0.29])
    audit.update(
        sensitivity_range_m=[float(min(sensitivity)), float(max(sensitivity))],
        sensitivity_basis="ring interface 0–1 px; hog line ±1 px; stone edge ±1 px; "
        "independent per camera; diagnostic range, not a confidence interval",
        nominal_within_sensitivity=bool(
            min(sensitivity) <= audit["nominal_tee_distance_m"] <= max(sensitivity)
        ),
    )
    calibration = {
        "id": spec["id"],
        "spec_sha256": digest(spec),
        "profile_sha256": digest(layout["views"]),
        "reference_frame_id": reference_id,
        "source_id": source["id"],
        "source_metadata_sha256": source["metadata_sha256"],
        "media_sha256": handle["sha256"],
        "review_basis": spec["review_basis"],
        "point_convention": spec["point_convention"],
        "pairs": pairs,
        "cameras": cameras,
        "distance_check": audit,
    }
    return calibration, frames


def publish_perspectives(db, report, ray, calibration, frames):
    if (
        ray["source_id"] != calibration["source_id"]
        or ray["media_sha256"] != calibration["media_sha256"]
        or ray["source_metadata_sha256"] != calibration["source_metadata_sha256"]
    ):
        raise ValueError("Perspective calibration disagrees with native ray source pins")
    document = copy.deepcopy({k: v for k, v in ray.items() if k != "id"})
    document.update(
        perspective_version=VERSION,
        perspective_base_ray_id=ray["id"],
        perspective_calibration=copy.deepcopy(calibration),
        perspectives={
            view: {"camera": copy.deepcopy(camera), "frame_id": ray["origin_frame_id"]}
            for view, camera in calibration["cameras"].items()
        },
    )
    split = ray["house_split"]
    document["delivery_split_proxy"] = {
        "status": split["status"],
        "elapsed_seconds": split.get("elapsed_seconds"),
        "nominal_mean_speed_mps": split.get("nominal_mean_speed_mps"),
        "basis": "Native source-frame passage across the nominal 12 ft delivery house",
        "interpretation": "Shorter split means faster passage; a delivery weight proxy, "
        "not a predicted stopping distance. The hand may still be pushing.",
    }
    plan_key = digest({"version": VERSION, "base_ray": ray["id"], "calibration": digest(calibration)})
    with db.snapshot_session(write=True) as session:
        revision = latest_revision(session, report["game_id"])
        if (
            revision.sha256 != report["review_sha256"]
            or source_or_error(session, ray["source_id"])["metadata_sha256"] != ray["source_metadata_sha256"]
        ):
            raise ValueError("Report or source changed during perspective analysis")
        base = session.get(SkillDeliveryRay, ray["id"])
        if base is None or digest(base.document) != base.sha256:
            raise ValueError("Native ray changed during perspective analysis")
        old = session.scalar(select(SkillDeliveryRay).where(SkillDeliveryRay.plan_key == plan_key))
        if old:
            if digest(old.document) != old.sha256:
                raise ValueError("Perspective-ray checksum mismatch")
            return {"id": old.id, "cached": True, "document": old.document}
        for eid, doc in frames.items():
            existing = session.scalar(select(Evidence).where(Evidence.evidence_key == doc["evidence_key"]))
            retained = existing.document if existing else doc
            target_id = existing.id if existing else eid
            if not existing:
                session.add(
                    Evidence(
                        id=eid, source_id=ray["source_id"], evidence_key=doc["evidence_key"], document=doc
                    )
                )
            cal = document["perspective_calibration"]
            if cal["reference_frame_id"] == eid:
                cal["reference_frame_id"] = target_id
            for pair in cal["pairs"]:
                if pair["frame_id"] == eid:
                    pair["frame_id"] = target_id
            document["frame_pins"][target_id] = digest(retained)
        row = SkillDeliveryRay(
            game_id=report["game_id"],
            candidate_id=ray["candidate_id"],
            plan_key=plan_key,
            document=document,
            sha256=digest(document),
        )
        session.add(row)
        session.flush()
        return {"id": row.id, "cached": False, "document": document}


def analyze_perspective_ray(db, game_id, request):
    report = estimate_report(db, game_id)
    if report["stale"]:
        raise ValueError("Refresh the player report before extending its rays")
    ray = next(
        (
            d.get("delivery_ray")
            for p in report["players"]
            for d in p["deliveries"]
            if d["candidate_id"] == request.candidate_id
        ),
        None,
    )
    if not ray:
        raise ValueError("Construct the native delivery-house ray first")
    spec = calibration_spec(request.calibration_id)
    prior = ray.get("perspective_calibration", {})
    if (
        ray.get("perspective_version") == VERSION
        and prior.get("spec_sha256") == digest(spec)
        and prior.get("profile_sha256") == digest(profile()["views"])
    ):
        return {"id": ray["id"], "cached": True, "document": {k: v for k, v in ray.items() if k != "id"}}
    with db.snapshot_session() as session:
        source = source_or_error(session, ray["source_id"])
    handle = resolve_media(source)
    calibration, frames = prepare_perspectives(db, source, handle, spec)
    return publish_perspectives(db, report, ray, calibration, frames)
