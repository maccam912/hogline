"""Layout-scoped odd/even direction rules and hierarchical segmentation proposals."""

from typing import Literal

from pydantic import Field

from hogline.adapters.sources import digest
from hogline.config import profile
from hogline.db import GameRow, Job, SkillDirectionRule
from hogline.domain.catalog import Conflict
from hogline.domain.models import Model
from hogline.domain.skills import SkillDocument, read_skills
from hogline.orchestration.skills import sequences_in_session
from hogline.reconstruction.skill_candidates import delivery_candidates
from hogline.reconstruction.skill_segments import segment_hierarchy


class DirectionRuleEdit(Model):
    expected_revision: int = Field(ge=0)
    odd_delivery_view: Literal["perspective_left", "perspective_right"] | None
    basis: str = Field(min_length=1, max_length=2000)


def layout_key(layout):
    # This metadata does not alter the processing profile or invalidate existing frames.
    return digest({"id": layout["id"], "views": layout["views"]})


def direction_rule(session, layout=None):
    layout = layout or profile()
    row = session.get(SkillDirectionRule, layout_key(layout))
    if row and digest(row.document) != row.sha256:
        raise ValueError("End-direction rule checksum mismatch")
    return {
        "revision": row.revision if row else 0,
        "layout_id": layout["id"],
        "layout_key": layout_key(layout),
        **(
            row.document
            if row
            else {"odd_delivery_view": None, "basis": "Odd-end delivery direction not assigned"}
        ),
    }


def save_direction_rule(db, edit):
    with db.snapshot_session(write=True) as session:
        layout = profile()
        current = direction_rule(session, layout)
        if current["revision"] != edit.expected_revision:
            raise Conflict("End-direction rule changed; reload before saving")
        doc = {
            "odd_delivery_view": edit.odd_delivery_view,
            "basis": edit.basis,
            "meaning": (
                "Odd ends: the thrower approaches the camera in this feed; the skip is at the far house "
                "in the opposite feed. Even ends reverse."
            ),
        }
        row = session.get(SkillDirectionRule, current["layout_key"])
        if row:
            row.document, row.sha256 = doc, digest(doc)
        else:
            session.add(SkillDirectionRule(id=current["layout_key"], document=doc, sha256=digest(doc)))
        session.flush()
        return direction_rule(session, layout)


def hierarchy_in_session(session, game_id):
    review = read_skills(session, game_id)
    document = SkillDocument.model_validate(review["document"])
    game = session.get(GameRow, game_id)
    sequences = sequences_in_session(session, game_id)
    layouts = {}
    current_layout = profile()
    for sequence in sequences:
        job = session.get(Job, sequence["job_id"])
        if job and digest(job.document.get("profile")) == sequence.get("profile_sha256"):
            layout = job.document["profile"]
            if layout_key(layout) == layout_key(current_layout):
                layouts[sequence["id"]] = layout
    return segment_hierarchy(
        {**game.document, "id": game.id},
        document,
        delivery_candidates(sequences, document.shots),
        sequences,
        layouts,
        direction_rule(session, current_layout),
        review["stale"],
    )
