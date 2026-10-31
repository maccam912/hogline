import pytest

from hogline.config import profile
from hogline.domain.skills import EndWindow, SkillDocument
from hogline.reconstruction.skill_candidates import delivery_candidates
from hogline.reconstruction.skill_segments import segment_hierarchy
from tests.test_skill_drafts import sequence


def delivery(id, seconds, view="perspective_right", near_camera=False, color="red"):
    doc = sequence(id, release=1, actor="A1")
    doc["samples"] = [{"source_seconds": seconds + i} for i in range(4)]
    doc["request"] = {"view": view}
    doc["proposal"]["deliveries"][0]["stone_color"] = color
    doc["proposal"]["activities"] = [
        {
            "actor_tag": "A1",
            "action": "delivery",
            "delivery_index": 0,
            "key_frame": 0,
            "bbox": [0.4, 0.65, 0.6, 0.95] if near_camera else [0.4, 0.35, 0.6, 0.6],
        }
    ]
    return doc


def hierarchy(sequences, document=None, spans=None):
    document = document or SkillDocument()
    return segment_hierarchy(
        {"id": "game", "spans": spans or [{"source_id": "source", "start_seconds": 0, "end_seconds": 5000}]},
        document,
        delivery_candidates(sequences, document.shots),
        sequences,
        {s["id"]: profile() for s in sequences},
        {"odd_delivery_view": "perspective_right"},
    )


def test_direction_runs_have_parity_but_do_not_invent_exact_ends_or_stop_times():
    h = hierarchy(
        [
            delivery("a", 10),
            delivery("b", 30),
            delivery("c", 90, "perspective_left"),
            delivery("d", 110, "perspective_left"),
        ]
    )
    assert [g["parity"] for g in h["end_groups"]] == ["odd", "even"]
    assert all(g["end"] is None for g in h["end_groups"])
    assert [len(g["deliveries"]) for g in h["end_groups"]] == [2, 2]
    boundary = h["end_groups"][1]["preceding_boundary"]
    assert boundary["earliest_seconds"] == 33 and boundary["latest_seconds"] == 90
    assert boundary["reason"] == "delivery_origin_changed"
    assert all(
        d["action_end_seconds"] is None and d["stone_end_seconds"] is None and d["team_delivery"] is None
        for g in h["end_groups"]
        for d in g["deliveries"]
    )


def test_duplicate_camera_sighting_does_not_create_an_end_change():
    right = delivery("a", 10)
    left = delivery("b", 10, "perspective_left", near_camera=True)
    h = hierarchy([right, left])
    assert h["delivery_count"] == 1
    assert h["end_groups"][0]["delivery_view"] == "perspective_right"
    left["proposal"]["activities"][0]["bbox"] = [0.4, 0.35, 0.6, 0.6]
    h = hierarchy([right, left])
    d = h["end_groups"][0]["deliveries"][0]
    assert d["origin"]["status"] == "conflicting_origins" and d["end_parity"] is None


def test_full_reviewed_end_gets_hierarchical_throw_labels_and_parity_conflicts_are_visible():
    seq = [delivery(str(i), 10 + i * 10, color="red" if i % 2 == 0 else "yellow") for i in range(16)]
    document = SkillDocument(
        end_windows=[
            EndWindow(
                id="w",
                source_id="source",
                start_seconds=0,
                end_seconds=200,
                end=1,
                first_side="red",
                acceptance="confirmed",
                basis="Reviewed",
                evidence=["source"],
            )
        ]
    )
    h = hierarchy(seq, document)
    assert len(h["end_groups"]) == 1
    throws = h["end_groups"][0]["deliveries"]
    assert (throws[0]["end"], throws[0]["side"], throws[0]["position"], throws[0]["stone_in_position"]) == (
        1,
        "red",
        "lead",
        1,
    )
    assert (throws[-1]["side"], throws[-1]["position"], throws[-1]["stone_in_position"]) == (
        "yellow",
        "fourth",
        2,
    )
    seq[-1]["request"]["view"] = "perspective_left"
    h = hierarchy(seq, document)
    assert len(h["end_groups"]) == 1
    assert "direction_parity_conflicts_with_reviewed_end" in h["end_groups"][0]["issues"]


def test_coverage_holes_unknown_origins_and_long_gaps_do_not_bridge_proposed_ends():
    h = hierarchy(
        [delivery("a", 10), delivery("b", 30)],
        spans=[
            {"source_id": "source", "start_seconds": 0, "end_seconds": 20},
            {"source_id": "source", "start_seconds": 25, "end_seconds": 50},
        ],
    )
    assert len(h["end_groups"]) == 2
    h = hierarchy([delivery("a", 10), delivery("b", 400)])
    assert len(h["end_groups"]) == 2
    unknown = delivery("b", 30)
    unknown["proposal"]["activities"] = []
    assert len(hierarchy([delivery("a", 10), unknown, delivery("c", 50)])["end_groups"]) == 3


def test_rule_is_shared_by_layout_without_mutating_processing_profile(db):
    from hogline.adapters.sources import digest
    from hogline.domain.catalog import Conflict
    from hogline.orchestration.skill_segmentation import (
        DirectionRuleEdit,
        direction_rule,
        save_direction_rule,
    )

    before = digest(profile())
    result = save_direction_rule(
        db,
        DirectionRuleEdit(
            expected_revision=0,
            odd_delivery_view="perspective_right",
            basis="Operator: right thrower toward camera; left skip at far house on odd ends",
        ),
    )
    assert result["revision"] == 1
    with db.snapshot_session() as session:
        assert direction_rule(session)["odd_delivery_view"] == "perspective_right"
        different = profile()
        different["views"]["perspective_right"] = [0.6, 0, 1, 0.95]
        assert direction_rule(session, different)["odd_delivery_view"] is None
    assert digest(profile()) == before
    with pytest.raises(Conflict):
        save_direction_rule(
            db, DirectionRuleEdit(expected_revision=0, odd_delivery_view="perspective_left", basis="stale")
        )


def test_hierarchy_and_direction_routes_expose_saved_mapping_without_accepting_ends(db, sources):
    from fastapi.testclient import TestClient

    from hogline.review.app import create_app
    from tests.test_skill_scope import make_game

    make_game(db, sources)
    client = TestClient(create_app(db.settings))
    rule = client.put(
        "/api/skills/end-direction",
        json={
            "expected_revision": 0,
            "odd_delivery_view": "perspective_right",
            "basis": "User-confirmed orientation",
        },
    )
    assert rule.status_code == 200, rule.text
    result = client.get("/api/games/game/skill-segments")
    assert result.status_code == 200, result.text
    h = result.json()
    assert h["game"]["coverage"] == "partial"
    assert h["direction_rule"]["odd_delivery_view"] == "perspective_right"
    assert h["end_groups"] == [] and h["completed_action_count"] == 0
