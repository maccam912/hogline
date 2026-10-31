"""Recompute bounded motion associations from retained detections, without decoding or remote calls."""

import time
from uuid import NAMESPACE_URL, uuid5

from hogline.adapters.sources import digest
from hogline.db import GameRow, Job, SkillMotion, now
from hogline.media.frames import EvidenceBudget, read_image, read_image_bytes, save_image
from hogline.orchestration.skill_calibration import checked_motion
from hogline.orchestration.skill_motion import motion_overlay
from hogline.perception.shot_motion import VERSION as TRACK_VERSION
from hogline.perception.shot_motion import (
    delivery_path_proposals,
    measure_track,
    motion_tracks,
    release_phase,
)

VERSION = "retained-motion-refinement-v1"


def refine_motion(db, motion_id):
    started = time.monotonic()
    with db.snapshot_session() as session:
        requested = session.get(SkillMotion, motion_id)
        if requested is None:
            raise ValueError("Motion evidence not found")
        game = session.get(GameRow, requested.game_id)
        requested = checked_motion(session, game, motion_id)
        if requested.document["version"] == TRACK_VERSION and "delivery_path_proposals" in requested.document:
            return {"motion_id": motion_id, "state": "succeeded", "reused": True}
        # Always derive directly from the immutable detection run, not a chain of refinements.
        parent_id = requested.document.get("retained_detection_parent", {}).get("motion_id", motion_id)
        parent = checked_motion(session, game, parent_id)
        doc, parent_sha = parent.document, parent.sha256
        parent_job = session.get(Job, parent.job_id)
        layout = (parent_job.document if parent_job else {}).get("profile")
        if not layout or digest(layout) != doc.get("profile_sha256"):
            raise ValueError("Retained motion needs its frozen camera profile")
        key = digest(
            {"method": VERSION, "tracking": TRACK_VERSION, "motion_id": parent_id, "sha256": parent_sha}
        )
        new_id = str(uuid5(NAMESPACE_URL, "refined-motion:" + key))
        job_id = str(uuid5(NAMESPACE_URL, "refined-motion-job:" + key))
        existing = session.get(SkillMotion, new_id)
        if existing:
            checked_motion(session, game, new_id)
            return {"motion_id": new_id, "state": "succeeded", "reused": True}
        game_id = game.id
    rows = doc["samples"]
    if not 3 <= len(rows) <= 160 or any(len(r["detections"]) > 128 for r in rows):
        raise ValueError("Retained motion exceeds the bounded association input")
    if any(max(r["crop_width"], r["crop_height"]) > 1080 for r in rows):
        raise ValueError("Retained motion exceeds supported crop dimensions")
    tracking = motion_tracks(rows, digest({"retained_detection_key": key, "samples": rows}))
    moving = [t for t in tracking["tracks"] if t["motion_class"] == "position_change_observed"]
    anchor = doc.get("delivery_anchor")
    for track in moving:
        track["relative_release_phase"] = release_phase(track, anchor["release_seconds"] if anchor else None)
        track["matches_delivery_color"] = (
            track["color"] == anchor["stone_color"] if anchor and anchor["stone_color"] != "unknown" else None
        )
    measurements = [measure_track(t, doc["projection"]) for t in moving]
    usable = any(m["status"] == "experimental_measurement" for m in measurements)
    reference = read_image(db.settings.data_dir, doc["reference_asset"])
    budget = EvidenceBudget(db.settings.data_dir, 16, db.settings.max_cache_mb)
    paths = delivery_path_proposals(moving, anchor, layout.get("id"))
    priority_ids = {p["track_id"] for p in paths if p["status"] == "review_first"}
    overlay = save_image(
        motion_overlay(reference, moving, priority_ids or None), db.settings.data_dir, budget
    )
    document = {
        **doc,
        "id": new_id,
        "job_id": job_id,
        "created_at": now(),
        "version": TRACK_VERSION,
        "tracking": tracking,
        "image_motion_tracks": moving,
        "measurements": measurements,
        "overlay_asset": overlay,
        "delivery_path_proposals": paths,
        "retained_detection_parent": {
            "motion_id": parent_id,
            "sha256": parent_sha,
            "version": doc["version"],
        },
        "refinement_version": VERSION,
        "status": "experimental_measurements" if usable else "image_motion_only" if moving else "unavailable",
    }
    elapsed = time.monotonic() - started
    if elapsed > 15:
        raise ValueError("Retained motion refinement exceeded its time budget")
    with db.snapshot_session(write=True) as session:
        game = session.get(GameRow, game_id)
        current = checked_motion(session, game, parent_id)
        if current.sha256 != parent_sha:
            raise ValueError("Retained detections changed before refinement publication")
        read_image_bytes(db.settings.data_dir, overlay)
        # A concurrent identical request may have completed while this one computed.
        if existing := session.get(SkillMotion, new_id):
            checked_motion(session, game, new_id)
            return {"motion_id": new_id, "state": "succeeded", "reused": True}
        session.add(
            Job(
                id=job_id,
                job_key=key,
                state="succeeded",
                attempts=1,
                document={
                    "kind": "skill_motion",
                    "processing_mode": "retained_detection_refinement",
                    "stage_version": VERSION,
                    "game_id": game_id,
                    "request": doc["request"],
                    "profile": layout,
                    "progress": 1,
                    "finished_at": now(),
                    "motion_id": new_id,
                    "wall_seconds": elapsed,
                    "parent_motion_id": parent_id,
                },
            )
        )
        session.flush()
        session.add(
            SkillMotion(id=new_id, game_id=game_id, job_id=job_id, document=document, sha256=digest(document))
        )
    return {"motion_id": new_id, "state": "succeeded", "reused": False}
