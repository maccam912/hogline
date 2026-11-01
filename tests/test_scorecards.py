import copy

import pytest
from fastapi.testclient import TestClient

from hogline.analytics.exports import snapshot
from hogline.analytics.lineage import validate_references
from hogline.reconstruction.scorecards import reconstruct_scorecards
from hogline.review.app import create_app
from tests.test_end_timeline import reading, repeated


def test_independent_rows_recover_complete_end_scores_without_simultaneous_readability():
    observations = repeated(0)
    for start, red, yellow, bad in [
        (20, ((1, 2), (3, 4)), (), "yellow"),
        (40, (), ((2, 1), (4, 4)), "red"),
    ]:
        rows = repeated(start, red=red, yellow=yellow)
        for row in rows:
            row["scoreboard"]["unreadable_slots"] = [{"color": bad}]
        observations += rows
    result = reconstruct_scorecards("source", observations)
    (book,) = result["scorecards"]
    assert book["score_history_complete_through"] == 4
    assert [e["points"] for e in book["ends"]] == [
        {"red": 2, "yellow": 0},
        {"red": 0, "yellow": 1},
        {"red": 2, "yellow": 0},
        {"red": 0, "yellow": 3},
    ]
    assert book["ends"][-1]["score_after_end"] == {"red": 4, "yellow": 4}
    assert book["game_completion"] == "unknown" and book["final_result"] is None
    assert result == reconstruct_scorecards("source", list(reversed(observations)))


def test_repeated_clear_board_separates_games_with_reused_end_numbers():
    rows = repeated(0, red=((1, 2),), yellow=((2, 1),))
    rows += repeated(20)
    rows += repeated(40, red=((2, 3),), yellow=((1, 4),))
    rows += repeated(60)
    cards = reconstruct_scorecards("source", rows)["scorecards"]
    assert len(cards) == 2
    assert cards[0]["ends"][-1]["score_after_end"] == {"red": 2, "yellow": 1}
    assert cards[1]["ends"][-1]["score_after_end"] == {"red": 3, "yellow": 4}
    assert cards[0]["left_censored"] and not cards[1]["left_censored"]
    assert all(not c["right_censored"] for c in cards)


def test_missing_end_is_not_a_blank_and_cannot_supply_a_false_point_delta():
    rows = repeated(0, red=((1, 2), (3, 5)), yellow=())
    (book,) = reconstruct_scorecards("source", rows)["scorecards"]
    assert book["score_history_complete_through"] is None
    assert book["ends"][1]["state"] == "unknown"
    assert book["ends"][2]["points"]["red"] is None
    assert book["ends"][2]["score_after_end"] == {"red": 5, "yellow": None}


def test_explicit_center_row_plate_supplies_blank_end_and_keeps_totals():
    rows = repeated(0, red=((1, 2), (3, 5)))
    for row in rows:
        row["scoreboard"]["blank_end_cards"] = [{"end_number": 2}]
    (book,) = reconstruct_scorecards("source", rows)["scorecards"]
    assert book["ends"][1]["state"] == "blank"
    assert book["ends"][1]["points"] == {"red": 0, "yellow": 0}
    assert book["ends"][2]["points"] == {"red": 3, "yellow": 0}
    assert book["score_history_complete_through"] == 3


def test_one_ocr_misread_does_not_overrule_repeated_card_and_competing_repeats_abstain():
    rows = repeated(0, red=((1, 2),)) + [reading(20, red=((1, 5),))]
    assert reconstruct_scorecards("source", rows)["scorecards"][0]["ends"][0]["points"]["red"] == 2
    rows += repeated(40, red=((1, 5),))
    (book,) = reconstruct_scorecards("source", rows)["scorecards"]
    assert book["card_conflicts"] and book["score_history_complete_through"] is None


def test_duplicate_frames_and_mixed_model_or_source_versions_cannot_add_votes():
    one = reading(0, red=((1, 2),))
    assert reconstruct_scorecards("source", [one, one, one])["scorecards"] == []
    rows = [reading(t, red=((1, 2),), model_version=f"reader-{t}") for t in (0, 5, 10)]
    assert reconstruct_scorecards("source", rows)["scorecards"] == []
    rows = repeated(0, red=((1, 2),))
    for i, row in enumerate(rows):
        row["source_metadata_sha256"] = str(i)
    assert reconstruct_scorecards("source", rows)["scorecards"] == []


def test_increasing_end_numbers_cannot_reduce_cumulative_points():
    rows = repeated(0, red=((1, 5), (2, 3)))
    (book,) = reconstruct_scorecards("source", rows)["scorecards"]
    assert book["ends"][1]["status"] == "unresolved"
    assert "cumulative_score_conflicts" in book["ends"][1]["unknown_reasons"][0]


def test_counter_does_not_force_immediate_posting_or_end_completion():
    rows = repeated(0)
    early = repeated(20, red=((1, 2),))
    for row in early:
        row["scoreboard"]["current_end"] = {"number": 3}
    late = copy.deepcopy(repeated(60, red=((1, 2),), yellow=((2, 1),)))
    result = reconstruct_scorecards("source", rows + early + late)
    (book,) = result["scorecards"]
    assert book["score_history_complete_through"] == 2
    assert book["ends"][1]["board_evidence"]["first"]["source_seconds"] == 60
    assert book["ends"][1]["last_stone_stopped"] is None


def test_delayed_empty_board_with_current_end_advanced_does_not_split_game():
    rows = repeated(0, red=((1, 2),))
    empty = repeated(20)
    for row in empty:
        row["scoreboard"]["current_end"] = {"number": 2}
    rows += empty + repeated(40, red=((1, 2),), yellow=((2, 1),))
    assert len(reconstruct_scorecards("source", rows)["scorecards"]) == 1


def test_reversed_plates_require_repeated_counts_and_unique_missing_ends():
    rows = repeated(0, red=((1, 2), (3, 4)))
    for row in rows:
        row["scoreboard"]["current_end"] = {"number": 4}
        row["scoreboard"]["unassigned_blank_plates"] = [
            {"column_hint": 2, "status": "reversed_blank_plate_candidate"}
        ]
    (book,) = reconstruct_scorecards("source", rows)["scorecards"]
    assert book["ends"][1]["state"] == "blank"
    assert book["ends"][2]["points"] == {"red": 2, "yellow": 0}
    assert book["score_history_complete_through"] == 3
    for row in rows:
        row["scoreboard"]["current_end"] = {"number": 2}
    (book,) = reconstruct_scorecards("source", rows)["scorecards"]
    assert book["ends"][1]["state"] == "unknown"


def test_scorecards_are_exposed_and_exported_with_checked_inputs(db, sources):
    client = TestClient(create_app(db.settings))
    response = client.get(f"/api/sources/{sources[0]['id']}/end-timeline").json()
    assert response["scorecard_reconstruction"]["summary"]["candidate_games"] == 0
    release = snapshot(db, "exploratory")
    assert len(release["scorecard_timelines"]) == 2
    validate_references(release)
    release["scorecard_timelines"][0]["summary"]["known_end_scores"] = 100
    with pytest.raises(ValueError, match="scorecard timelines"):
        validate_references(release)


def test_repeated_ocr_alias_in_same_column_does_not_invent_later_ends():
    # March 8 Sheet 5: 44 readings of red end 3 at cumulative 4, four misreads as 8.
    rows = repeated(0)
    for t in range(20, 240, 5):
        rows.append(reading(t, red=((2, 1), (3, 4), (5, 5)), yellow=((1, 4), (4, 5))))
    for t in range(240, 260, 5):
        rows.append(reading(t, red=((2, 1), (8, 4), (5, 5)), yellow=((1, 4), (4, 5))))
    current = reconstruct_scorecards("source", rows)
    (book,) = current["scorecards"]
    assert book["highest_posted_end"] == 5
    assert book["ends"][-1]["score_after_end"] == {"red": 5, "yellow": 5}
    rejected = book["ends"][2]["board_evidence"]["rejected_column_aliases"]
    assert rejected[0]["official_number"] == 8 and rejected[0]["supporting_frames"] == 4
    assert len(rejected[0]["evidence_ids"]) == 4
    legacy = reconstruct_scorecards("source", rows, version="temporal-scorecards-v2")
    assert legacy["scorecards"][0]["highest_posted_end"] == 8
    assert current == reconstruct_scorecards("source", list(reversed(rows)))


def test_comparable_column_aliases_abstain_and_blanks_do_not_share_scoring_columns():
    rows = repeated(0, red=((1, 2),)) + repeated(20, red=((3, 2),))
    (book,) = reconstruct_scorecards("source", rows)["scorecards"]
    assert book["card_conflicts"][0]["official_numbers"] == [1, 3]
    assert not book["ends"] and book["score_history_complete_through"] is None
    rows = repeated(0, red=((1, 2), (4, 3)))
    for row in rows:
        row["scoreboard"]["blank_end_cards"] = [{"end_number": 2}, {"end_number": 3}]
    (book,) = reconstruct_scorecards("source", rows)["scorecards"]
    assert book["score_history_complete_through"] == 4
    assert [e["state"] for e in book["ends"]] == ["played", "blank", "blank", "played"]


def test_legacy_scorecard_release_reproduces_with_pinned_version(db):
    release = snapshot(db, "exploratory")
    for report in release["scorecard_timelines"]:
        report.update(reconstruct_scorecards(report["source_id"], [], version="temporal-scorecards-v2"))
    validate_references(release)
