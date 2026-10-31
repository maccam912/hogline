from hogline.domain.skills import Shot
from hogline.reconstruction.skill_candidates import delivery_candidates
from hogline.reconstruction.skill_drafts import candidate_coverage, candidate_review


def sequence(identifier, release=None, actor=None):
    return {
        "id": identifier,
        "created_at": identifier,
        "source_id": "source",
        "samples": [{"source_seconds": n} for n in range(4)],
        "proposal": {
            "deliveries": [
                {
                    "first_frame": 0,
                    "last_frame": 3,
                    "release_frame": release,
                    "stone_color": "red",
                    "call": "unknown",
                    "thrower_description": "Gray shirt",
                    "thrower_actor_tag": actor,
                }
            ]
        },
    }


def review(sequences, shots=()):
    candidate = delivery_candidates(sequences, shots)[0]
    candidate["review"] = candidate_review(candidate, shots)
    return candidate


def test_new_draft_combines_evidence_without_accepting_unknowns_or_measurements():
    candidate = review([sequence("a"), sequence("b", release=2, actor="A1")])
    result = candidate["review"]
    draft = Shot.model_validate(result["draft"])
    assert result["status"] == "new"
    assert draft.seconds == 2 and draft.participant == "skill-actor:b:A1"
    assert len(draft.evidence) == 4
    assert draft.acceptance == "proposed" and draft.end is None
    assert draft.call == "unknown" and draft.team_delivery is None
    assert draft.speed_mps is None and draft.line_error_degrees is None
    assert result["release_proposals_seconds"] == [2]
    assert candidate_coverage([candidate])["unreviewed"] == 1


def test_existing_operator_decisions_are_preserved_even_when_model_disagrees():
    saved = Shot(
        id="saved",
        source_id="source",
        seconds=1,
        end=2,
        side="yellow",
        call="guard",
        acceptance="confirmed",
        participant="Yellow lead",
        note="Reviewed from the recording",
        evidence=["skill-sequence:a:frame:0"],
    )
    candidate = review([sequence("a"), sequence("b", release=2, actor="A1")], [saved])
    draft = candidate["review"]["draft"]
    assert {k: v for k, v in draft.items() if k != "evidence"} == saved.model_dump(exclude={"evidence"})
    assert len(draft["evidence"]) == 4
    assert candidate_coverage([candidate])["with_saved_review"] == 1


def test_excluded_candidate_is_not_resurrected_as_a_new_proposal():
    saved = Shot(
        id="excluded",
        source_id="source",
        seconds=1,
        acceptance="excluded",
        evidence=["skill-sequence:a:frame:0"],
    )
    candidate = review([sequence("a")], [saved])
    assert candidate["review"]["status"] == "excluded"
    assert candidate["review"]["draft"]["id"] == "excluded"
    assert candidate["review"]["draft"]["acceptance"] == "excluded"
    assert candidate_coverage([candidate])["excluded"] == 1


def test_multiple_saved_reviews_require_resolution_and_reference_count_is_bounded():
    saved = [
        Shot(id=name, source_id="source", seconds=1, evidence=["skill-sequence:a:frame:0"])
        for name in ("one", "two")
    ]
    candidate = review([sequence("a")], saved)
    assert candidate["review"]["draft"] is None
    assert candidate_coverage([candidate])["duplicate_review"] == 1
    candidate = review([sequence(str(n)) for n in range(20)])
    assert len(candidate["review"]["draft"]["evidence"]) == 32
    assert candidate["review"]["unattached_reference_count"] == 8
