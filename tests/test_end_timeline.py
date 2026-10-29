import copy
import json

import pytest
from fastapi.testclient import TestClient

from hogline.adapters.sources import digest
from hogline.analytics.end_timeline import game_end_timeline
from hogline.analytics.exports import snapshot
from hogline.analytics.lineage import validate_references
from hogline.domain.catalog import save_game
from hogline.domain.models import Game, GameEdit, Span
from hogline.reconstruction.ends import paired_epochs, reconstruct_ends
from hogline.review.app import create_app


def reading(seconds, red=(), yellow=(), **changes):
    return {
        "id": f"obs-{seconds}",
        "evidence_id": f"frame-{seconds}",
        "source_id": "source",
        "source_metadata_sha256": "manifest",
        "model_version": "reader-v1",
        "source_seconds": seconds,
        "youtube_url": f"https://example.invalid/?t={seconds}",
        "scoreboard": {
            "status": "observed",
            "cards": [
                {"color": color, "end_number": n, "cumulative_points": p}
                for color, row in [("red", red), ("yellow", yellow)]
                for n, p in row
            ],
            "unreadable_slots": [],
            "conflicting_end_numbers": [],
        },
        **changes,
    }


def repeated(start, **kwargs):
    return [reading(start + i * 5, **kwargs) for i in range(3)]


def test_scores_after_ends_keep_missing_baselines_blanks_and_final_count_unknown():
    rows = (
        repeated(0, red=((1, 2),), yellow=((2, 1),))
        + repeated(20, red=((1, 2), (3, 4)), yellow=((2, 1),))
        + repeated(40, red=((1, 2), (3, 4)), yellow=((2, 1), (5, 3)))
    )
    timeline = reconstruct_ends("source", rows)
    assert json.loads(json.dumps(timeline)) == reconstruct_ends("source", rows)
    (book,) = timeline["board_ledgers"]
    assert book["highest_observed_end_number"] == 5
    assert book["ends_played"] is None and book["scheduled_ends"] is None
    assert book["final_result"] is None and book["game_completion"] == "unknown"
    first, second, third, fourth, fifth = book["ends"]
    assert first["points"] == {"red": None, "yellow": None}
    assert first["score_after_end"] == {"red": 2, "yellow": None}
    assert second["score_after_end"] == {"red": 2, "yellow": 1}
    assert third["points"] == {"red": 2, "yellow": 0}
    assert third["score_after_end"] == {"red": 4, "yellow": 1}
    assert fourth["status"] == "unknown" and fourth["points"] == {"red": None, "yellow": None}
    assert fifth["points"] == {"red": 0, "yellow": 2}
    assert fifth["score_after_end"] == {"red": 4, "yellow": 3}
    posting = third["board_posting"]
    assert (posting["earliest_seconds"], posting["latest_seconds"]) == (10, 20)
    assert first["board_posting"]["left_censored"]
    assert all(
        e["first_delivery"] is None and e["last_stone_stopped"] is None and e["score_decided"] is None
        for e in book["ends"]
    )
    assert timeline == reconstruct_ends("source", list(reversed(rows)))


def test_reset_correction_and_reader_changes_never_mix_game_score_histories():
    rows = repeated(0, red=((1, 2), (3, 4)), yellow=((2, 1),))
    rows += repeated(30, red=((1, 1),), yellow=())
    result = reconstruct_ends("source", rows)
    before, after = result["board_ledgers"]
    assert before["highest_observed_end_number"] == 3
    assert after["highest_observed_end_number"] == 1
    assert after["boundary_reason"] == "board_reset_or_correction"
    assert after["ends"][0]["points"]["red"] is None
    for key in ["source_metadata_sha256", "model_version"]:
        changed = repeated(30, red=((1, 2), (3, 4)), yellow=((2, 1),), **{key: "other"})
        assert len(reconstruct_ends("source", rows[:3] + changed)["board_ledgers"]) == 2


def test_both_rows_need_shared_distinct_frames_conflicting_cards_and_unknowns_abstain():
    rows = repeated(0, red=((1, 2),))
    for row in rows:
        row["scoreboard"]["unreadable_slots"] = [{"color": "yellow"}]
    assert paired_epochs(rows) == []
    assert paired_epochs([reading(0), reading(0), reading(0)]) == []
    bad = repeated(0, red=((1, 3), (2, 2)))
    assert reconstruct_ends("source", bad)["board_ledgers"] == []
    both = repeated(0, red=((1, 3),), yellow=((1, 2),))
    assert reconstruct_ends("source", both)["board_ledgers"] == []
    empty = reconstruct_ends("source", repeated(0))
    assert empty["board_ledgers"][0]["ends"] == []
    assert empty["board_ledgers"][0]["highest_observed_end_number"] is None


def physical_result():
    def anchor(t):
        return {
            "source_seconds": t,
            "decoded_rgb_sha256": f"rgb-{t}",
            "pts": int(t * 10),
            "time_base_num": 1,
            "time_base_den": 10,
            "youtube_url": None,
        }

    before = {"id": "before", "start_seconds": 15, "end_seconds": 18, "first": anchor(15), "last": anchor(18)}
    after = {"id": "after", "start_seconds": 25, "end_seconds": 28, "first": anchor(25), "last": anchor(28)}
    result = {
        "id": "house",
        "window_id": "window",
        "source_id": "source",
        "source_metadata_sha256": "manifest",
        "profile_sha256": "profile",
        "version": "sampled-house-states-v1",
        "status": "derived",
        "states": [before, after],
        "transitions": [
            {
                "id": "clear",
                "view": "house_top",
                "kind": "possible_clearing",
                "before_state_id": "before",
                "after_state_id": "after",
                "earliest_seconds": 18,
                "latest_seconds": 25,
                "before": anchor(18),
                "after": anchor(25),
            }
        ],
    }
    return {"sha256": digest(result), **result}


def test_house_board_comparison_needs_unique_temporal_association_and_exact_frame():
    rows = repeated(0, red=((1, 2),), yellow=((2, 1),)) + repeated(30, red=((1, 2), (3, 4)), yellow=((2, 1),))
    physical = physical_result()
    score = {
        "id": "score",
        "source_id": "source",
        "source_metadata_sha256": "manifest",
        "profile_sha256": "profile",
        "decoded_rgb_sha256": "rgb-18",
        "views": {"house_top": {"scoring": {"provisional_score": {"red": 2, "yellow": 0}}}},
    }
    timeline = reconstruct_ends("source", rows, [physical], [score])
    (candidate,) = timeline["end_candidates"]
    (comparison,) = candidate["board_comparisons"]
    assert candidate["official_end_number"] == 3
    assert comparison["comparison"] == "agrees" and comparison["posting_lag_seconds"] is None
    mismatch = copy.deepcopy(score)
    mismatch["views"]["house_top"]["scoring"]["provisional_score"] = {"red": 1, "yellow": 0}
    assert (
        reconstruct_ends("source", rows, [physical], [mismatch])["end_candidates"][0]["board_comparisons"][0][
            "comparison"
        ]
        == "disagrees"
    )
    wrong = {**score, "decoded_rgb_sha256": "another-frame"}
    assert not reconstruct_ends("source", rows, [physical], [wrong])["end_candidates"][0][
        "house_score_proposals"
    ]
    other = copy.deepcopy(physical)
    other["id"] = "other-house"
    other["transitions"][0]["view"] = "house_bottom"
    other["transitions"][0]["before"]["decoded_rgb_sha256"] = "different"
    result = reconstruct_ends("source", rows, [physical, other])
    assert all(c["official_end_number"] is None for c in result["end_candidates"])
    assert all(c["board_comparisons"][0]["association"] == "ambiguous" for c in result["end_candidates"])
    assert not reconstruct_ends("source", rows, [physical], scope=(30, 50))["end_candidates"]


def test_api_game_scopes_export_and_tamper_detection(db, sources):
    game = Game(sheet=5, spans=[Span(source_id=sources[0]["id"], start_seconds=100, end_seconds=200)])
    saved = save_game(db, GameEdit(expected_revision=0, author="Tester", reason="Known game span", game=game))
    result = game_end_timeline(db, saved["id"])
    assert result["game_revision"] == saved["revision"]
    assert result["ends_played"] is None and result["source_timelines"][0]["scope"] == [100, 200]
    client = TestClient(create_app(db.settings))
    assert client.get(f"/api/games/{saved['id']}/end-timeline").json() == result
    assert client.get(f"/api/sources/{sources[0]['id']}/end-timeline?start=3&end=2").status_code == 422
    assert client.get(f"/api/sources/{sources[0]['id']}/end-timeline?start=3").status_code == 422
    release = snapshot(db, "exploratory")
    assert len(release["end_timelines"]) == 2
    validate_references(release)
    release["end_timelines"][0]["end_candidates"] = [{"invented": True}]
    with pytest.raises(ValueError, match="end timelines"):
        validate_references(release)
