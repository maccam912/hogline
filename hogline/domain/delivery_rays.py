"""Read only current, pinned delivery-ray proposals."""

from sqlalchemy import select

from hogline.adapters.sources import digest
from hogline.db import Evidence, SkillDeliveryRay
from hogline.domain.catalog import source_or_error
from hogline.media.frames import read_image_bytes


def current_rays(session, root, game_id, review_sha256):
    result = {}
    seen = set()
    checked_frames = {}
    for row in session.scalars(
        select(SkillDeliveryRay)
        .where(SkillDeliveryRay.game_id == game_id)
        .order_by(SkillDeliveryRay.created_at.desc(), SkillDeliveryRay.id.desc())
    ):
        d = row.document
        if d.get("review_sha256") != review_sha256:
            continue
        if row.candidate_id in seen:
            continue
        seen.add(row.candidate_id)
        if digest(d) != row.sha256:
            continue
        if source_or_error(session, d["source_id"])["metadata_sha256"] != d["source_metadata_sha256"]:
            continue
        valid = True
        for eid, sha in d["frame_pins"].items():
            if (eid, sha) in checked_frames:
                if not checked_frames[eid, sha]:
                    valid = False
                    break
                continue
            frame = session.get(Evidence, eid)
            if frame is None or digest(frame.document) != sha:
                checked_frames[eid, sha] = False
                valid = False
                break
            try:
                read_image_bytes(root, frame.document["image"])
            except ValueError:
                checked_frames[eid, sha] = False
                valid = False
                break
            checked_frames[eid, sha] = True
        if valid:
            result[row.candidate_id] = {"id": row.id, **d}
    return result
