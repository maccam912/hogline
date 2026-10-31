import copy

import pytest
from pydantic import ValidationError

from hogline.perception.shot_sequence import (
    ActivityProposal,
    ActivitySequenceProposal,
    validate_frame_references,
)
from hogline.reconstruction.skill_actors import actor_proposals


def activity(**changes):
    return {
        "actor_tag": "A1",
        "description": "Gray jacket at left",
        "first_frame": 0,
        "last_frame": 2,
        "key_frame": 1,
        "bbox": [0.1, 0.1, 0.3, 0.9],
        "action": "calling_broom",
        "team_side": "unknown",
        "side_basis": "No team established",
        "stone_color": "unknown",
        "delivery_index": None,
        "evidence_basis": "Points broom to ice",
        "limitations": [],
        **changes,
    }


def sequence():
    return {
        "id": "sequence-1",
        "source_id": "source",
        "samples": [{"source_seconds": t} for t in [10, 11, 12]],
        "proposal": {"activities": [activity()], "deliveries": []},
    }


def test_clip_scoped_roles_never_infer_skip_or_join_matching_clothing():
    first, second = sequence(), sequence()
    second["id"] = "sequence-2"
    actors = actor_proposals([first, second])
    assert len(actors) == 2 and actors[0]["id"] != actors[1]["id"]
    assert actors[0]["role_proposals"] == ["calling_broom_holder"]
    assert all(a["skip"] is None and a["throwing_position"] is None for a in actors)
    first["stale"] = True
    assert len(actor_proposals([first, second])) == 1


def test_conflicting_side_claims_survive_as_unknown():
    doc = sequence()
    doc["proposal"]["activities"] = [
        activity(team_side="red"),
        activity(team_side="yellow", action="sweeping"),
    ]
    actor = actor_proposals([doc])[0]
    assert actor["side_conflict"] and actor["team_side"] == "unknown"
    assert actor["role_proposals"] == ["sweeper", "calling_broom_holder"]


def test_activity_bounds_and_references_are_validated():
    for changes in [
        {"bbox": [0.4, 0.1, 0.3, 0.5]},
        {"key_frame": 3},
        {"action": "sweeping", "first_frame": 1, "last_frame": 1},
        {"team_side": "red", "side_basis": ""},
    ]:
        with pytest.raises(ValidationError):
            ActivityProposal.model_validate(activity(**changes))
    proposal = ActivitySequenceProposal(
        call="unknown",
        call_basis="",
        broom_visible=True,
        delivery_visible=False,
        sweeping_visible=False,
        observations=[],
        limitations=[],
        deliveries=[],
        activities=[activity()],
    )
    with pytest.raises(ValueError, match="frame not supplied"):
        validate_frame_references(proposal, 2)
    invalid = copy.deepcopy(proposal.model_dump())
    invalid["activities"][0]["delivery_index"] = 0
    with pytest.raises(ValidationError, match="absent delivery"):
        ActivitySequenceProposal.model_validate(invalid)
