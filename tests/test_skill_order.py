import itertools

import pytest
from pydantic import ValidationError

from hogline.domain.skills import EndWindow, Shot, SkillDocument, SkillEdit, save_skills
from hogline.reconstruction.skill_candidates import delivery_candidates
from hogline.reconstruction.skill_order import order_possibilities, order_report
from tests.test_skill_drafts import sequence
from tests.test_skill_scope import make_game


def event(side, number=None):
    return {"side": side, "team_delivery": number}


def window(**changes):
    return EndWindow(
        id="w",
        source_id="source",
        start_seconds=0,
        end_seconds=200,
        end=1,
        acceptance="confirmed",
        basis="Reviewed full end boundaries",
        evidence=["video:0-200"],
        **changes,
    )


def test_partial_alternating_excerpt_does_not_become_lead():
    rows = order_possibilities([event("red"), event("yellow"), event("red")], "red")
    assert {v["team_delivery"] for v in rows[0]} == set(range(1, 8))
    assert {v["position"] for v in rows[0]} == {"lead", "second", "third", "fourth"}
    assert {v["team_delivery"] for v in rows[-1]} == set(range(2, 9))


def test_full_end_and_confirmed_anchors_determine_order():
    events = [event("red" if i % 2 == 0 else "yellow") for i in range(16)]
    rows = order_possibilities(events)
    assert all(len(v) == 1 for v in rows)
    assert rows[-1][0] == {"side": "yellow", "team_delivery": 8, "overall_delivery": 16, "position": "fourth"}
    rows = order_possibilities([event("red", 3), event("unknown"), event("red", 4)], "red")
    assert rows[1] == [{"side": "yellow", "team_delivery": 3, "overall_delivery": 6, "position": "second"}]


def test_missing_throw_can_skip_indices_and_contradictions_have_no_solution():
    rows = order_possibilities([event("red", 2), event("red", 3)], "red")
    assert [r[0]["overall_delivery"] for r in rows] == [3, 5]
    assert order_possibilities([event("red", 3), event("yellow", 1)]) == [[], []]
    assert all(not v for v in order_possibilities([event("unknown")] * 17))


def test_dp_matches_exhaustive_paths_for_short_sequences():
    # Independent exhaustive oracle catches forward/backward constraint propagation.
    for colors in itertools.product(("red", "yellow", "unknown"), repeat=3):
        events = [event(c, 4 if i == 1 else None) for i, c in enumerate(colors)]
        expected = [set() for _ in events]
        for first in ("red", "yellow"):
            for path in itertools.combinations(range(1, 17), 3):
                values = [
                    (first if n % 2 else ("yellow" if first == "red" else "red"), (n + 1) // 2, n)
                    for n in path
                ]
                if all(
                    e["side"] in ("unknown", side)
                    and (e["team_delivery"] is None or e["team_delivery"] == number)
                    for e, (side, number, _) in zip(events, values, strict=True)
                ):
                    for result, value in zip(expected, values, strict=True):
                        result.add(value)
        actual = order_possibilities(events)
        assert [
            {(v["side"], v["team_delivery"], v["overall_delivery"]) for v in r} for r in actual
        ] == expected


def candidates():
    a = sequence("a")
    b = sequence("b")
    b["samples"] = [{"source_seconds": i + 20} for i in range(4)]
    return delivery_candidates([a, b])


def test_report_uses_only_confirmed_shot_numbers_and_preserves_skip_unknown():
    shot = Shot(
        id="anchor",
        source_id="source",
        seconds=1,
        end=1,
        side="red",
        team_delivery=3,
        acceptance="confirmed",
        evidence=["skill-sequence:a:frame:0"],
        participant="Red second",
    )
    a = sequence("a")
    b = sequence("b")
    b["samples"] = [{"source_seconds": i + 20} for i in range(4)]
    cs = delivery_candidates([a, b], [shot])
    result = order_report(cs, SkillDocument(end_windows=[window()], shots=[shot]))
    entries = result["windows"][0]["entries"]
    assert entries[0]["proposed_team_delivery"] == 3
    assert entries[0]["proposed_position"] == "second" and entries[0]["skip"] is None
    assert entries[1]["team_delivery_options"] == list(range(4, 9))
    assert len(result["search_leads"]) == 1
    proposed = shot.model_copy(update={"acceptance": "proposed"})
    result = order_report(cs, SkillDocument(end_windows=[window()], shots=[proposed]))
    assert result["windows"][0]["entries"][0]["proposed_team_delivery"] is None


def test_unreviewed_stale_and_crossing_intervals_withhold_order():
    document = SkillDocument(end_windows=[window().model_copy(update={"acceptance": "proposed"})])
    result = order_report(candidates(), document)
    assert result["windows"][0]["issues"] == ["unconfirmed_end_interval"]
    assert all(not e["possibilities"] for e in result["windows"][0]["entries"])
    document.end_windows[0] = window()
    assert "stale_skill_review" in order_report(candidates(), document, True)["windows"][0]["issues"]
    document.end_windows[0] = window().model_copy(update={"start_seconds": 2})
    assert "delivery_crosses_start_boundary" in order_report(candidates(), document)["windows"][0]["issues"]


def test_review_window_validation_and_source_scope(db, sources):
    with pytest.raises(ValidationError, match="evidence"):
        EndWindow(
            id="x",
            source_id="s",
            start_seconds=0,
            end_seconds=10,
            end=1,
            acceptance="confirmed",
            basis="Reviewed",
        )
    with pytest.raises(ValidationError, match="overlap"):
        SkillDocument(end_windows=[window(), window().model_copy(update={"id": "two", "end": 2})])
    make_game(db, sources)
    w = window().model_copy(update={"source_id": sources[0]["id"]})
    with pytest.raises(ValueError, match="game source span"):
        save_skills(db, "game", SkillEdit(expected_revision=0, document=SkillDocument(end_windows=[w])))
    w = w.model_copy(update={"start_seconds": 10, "end_seconds": 100})
    saved = save_skills(db, "game", SkillEdit(expected_revision=0, document=SkillDocument(end_windows=[w])))
    assert saved["document"]["end_windows"][0]["end"] == 1


def test_sweeping_cue_targets_preceding_camera_clip(db, sources):
    from hogline.adapters.sources import digest
    from hogline.db import Job, SkillSequence
    from hogline.orchestration.skill_order import gap_search_plan
    from tests.test_skill_house_context import survey

    seq = survey(db, sources)
    doc = seq["document"]
    doc["proposal"] = {
        "activities": [
            {
                "action": "sweeping",
                "stone_color": "yellow",
                "actor_tag": "A1",
                "first_frame": 5,
                "bbox": [0.7, 0.1, 0.9, 0.8],
            }
        ]
    }
    lead = {"side": "red", "source_id": doc["source_id"], "start_seconds": 1, "end_seconds": 15}
    with db.snapshot_session(write=True) as session:
        row = session.get(SkillSequence, doc["id"])
        row.document = doc
        row.sha256 = digest(doc)
    with db.snapshot_session() as session:
        plan = gap_search_plan(session, lead, [doc])
        assert plan["request"]["view"] == "perspective_right"
        assert (plan["request"]["start_seconds"], plan["request"]["end_seconds"]) == (2, 14)
        assert plan["request"]["interval_seconds"] == 0.75
        assert plan["anchor_sha256"] == digest(doc)
        assert gap_search_plan(session, {**lead, "side": "yellow"}, [doc]) is None
        assert gap_search_plan(session, lead, [{**doc, "stale": True}]) is None
        assert (
            gap_search_plan(session, lead, [{k: v for k, v in doc.items() if k != "profile_sha256"}]) is None
        )
    with db.snapshot_session(write=True) as session:
        session.get(Job, "job").document = {"profile": {"changed": True}}
    with db.snapshot_session() as session:
        assert gap_search_plan(session, lead, [doc]) is None
