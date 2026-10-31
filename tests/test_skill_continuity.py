import copy

import pytest

from hogline.analytics.skills import skill_report
from hogline.domain.skills import Participant, Shot, SkillDocument, Slot, role_proposals
from hogline.reconstruction.skill_continuity import continuity_proposals


def sequence(identifier, source="source", image_hash="same-frame", bounds=None, box=None):
    return {
        "id": identifier,
        "source_id": source,
        "stale": False,
        "samples": [
            {
                "source_seconds": t,
                "decoded_rgb_sha256": image_hash + str(t),
                "normalized_crop_bounds": bounds or [0, 0, 1, 1],
            }
            for t in (10, 11, 12)
        ],
        "proposal": {
            "activities": [
                {
                    "actor_tag": "A1",
                    "first_frame": 0,
                    "last_frame": 2,
                    "key_frame": 1,
                    "bbox": box or [0.4, 0.1, 0.5, 0.3],
                    "description": "Gray top",
                    "action": "delivery",
                    "team_side": "red",
                    "side_basis": "Controls stone",
                    "stone_color": "red",
                    "delivery_index": 0,
                    "evidence_basis": "Visible slide",
                    "limitations": [],
                }
            ]
        },
    }


def participant(**changes):
    return Participant(
        id="skill-player:p1",
        label="Anonymous player 1",
        actor_ids=["skill-actor:a:A1", "skill-actor:b:A1"],
        basis="Reviewed matching delivery observations",
        **changes,
    )


def shots():
    return [
        Shot(
            id=str(n),
            source_id="source",
            seconds=10 + n,
            end=1,
            side="red",
            team_delivery=n + 1,
            participant=f"skill-actor:{name}:A1",
            acceptance="confirmed",
            call="draw",
            turn="left",
            direction="near_to_far",
            speed_mps=1.8 + 0.4 * n,
            target_speed_mps=2,
            calibration="test-plane",
            evidence=["reviewed-frame"],
        )
        for n, name in enumerate(("a", "b"))
    ]


def test_only_confirmed_continuity_pools_shots_and_role_proposals():
    document = SkillDocument(participants=[participant()], shots=shots())
    assert len(skill_report(document)["summaries"]) == 2
    assert role_proposals(document)[0]["status"] == "conflict"
    document.participants[0].acceptance = "confirmed"
    report = skill_report(document)
    assert len(report["summaries"]) == 1
    assert report["summaries"][0]["participant"] == "skill-player:p1"
    assert report["summaries"][0]["weight"]["mean"] == 1
    assert role_proposals(document)[0]["status"] == "proposed"
    document.participants[0].acceptance = "excluded"
    assert len(skill_report(document)["summaries"]) == 2


def test_unreviewed_canonical_participant_is_excluded_and_slot_aliases_agree():
    document = SkillDocument(participants=[participant()], shots=[shots()[0]])
    document.shots[0].participant = "skill-player:p1"
    assert skill_report(document)["excluded"] == {"unreviewed_participant_continuity": 1}
    document.participants[0].acceptance = "confirmed"
    document.slots = [
        Slot(end=1, side="red", position="lead", participant="skill-actor:b:A1", acceptance="confirmed")
    ]
    assert skill_report(document)["excluded"] == {}


def test_duplicate_confirmed_memberships_and_positions_are_rejected():
    first = participant(acceptance="confirmed")
    second = first.model_copy(update={"id": "skill-player:p2"})
    with pytest.raises(ValueError, match="two confirmed"):
        SkillDocument(participants=[first, second])
    with pytest.raises(ValueError, match="two positions"):
        SkillDocument(
            participants=[first],
            slots=[
                Slot(
                    end=1, side="red", position="lead", participant="skill-actor:a:A1", acceptance="confirmed"
                ),
                Slot(
                    end=1,
                    side="red",
                    position="second",
                    participant="skill-actor:b:A1",
                    acceptance="confirmed",
                ),
            ],
        )
    with pytest.raises(ValueError, match="absent game participant"):
        SkillDocument(shots=[shots()[0].model_copy(update={"participant": "skill-player:absent"})])


def test_exact_frame_and_crop_geometry_suggest_continuity_without_face_matching():
    a = sequence("a")
    b = sequence("b", bounds=[0.375, 0, 0.625, 0.4], box=[0.1, 0.25, 0.5, 0.75])
    result = continuity_proposals([a, b], [], SkillDocument())
    assert len(result) == 1 and result[0]["acceptance"] == "proposed"
    assert result[0]["evidence"][0]["kind"] == "same_frame_body_overlap"
    assert result[0]["evidence"][0]["box_iou"] == pytest.approx(1)
    for other in (
        sequence("b", source="other"),
        sequence("b", image_hash="different"),
        sequence("b", box=[0.7, 0.1, 0.9, 0.3]),
    ):
        assert continuity_proposals([a, other], [], SkillDocument()) == []
    b["stale"] = True
    assert continuity_proposals([a, b], [], SkillDocument()) == []


def test_competing_same_frame_people_and_already_confirmed_links_are_not_resuggested():
    a, b = sequence("a"), sequence("b")
    duplicate = copy.deepcopy(b["proposal"]["activities"][0])
    duplicate["actor_tag"] = "A2"
    b["proposal"]["activities"].append(duplicate)
    assert continuity_proposals([a, b], [], SkillDocument()) == []
    assert (
        continuity_proposals(
            [a, sequence("b")], [], SkillDocument(participants=[participant(acceptance="confirmed")])
        )
        == []
    )


def test_same_delivery_proposes_links_across_frames_but_not_ambiguous_events():
    a, b = sequence("a"), sequence("b", image_hash="different")
    candidate = {
        "id": "delivery",
        "source_id": "source",
        "start_seconds": 10,
        "end_seconds": 12,
        "ambiguous_overlap": False,
        "duplicate_review": False,
        "observations": [{"thrower_actor_id": f"skill-actor:{s}:A1"} for s in ("a", "b")],
    }
    result = continuity_proposals([a, b], [candidate], SkillDocument())
    assert len(result) == 1 and result[0]["evidence"][0]["kind"] == "same_delivery_proposal"
    candidate["ambiguous_overlap"] = True
    assert continuity_proposals([a, b], [candidate], SkillDocument()) == []
