import copy

import pytest

from hogline.reconstruction.board_events import board_timeline


def observation(seconds, red=((1, 2),), yellow=((2, 1),), **changes):
    return {
        "id": f"observation-{seconds}",
        "evidence_id": f"frame-{seconds}",
        "source_id": "source-1",
        "source_metadata_sha256": "manifest-1",
        "model_version": "reader-1",
        "source_seconds": seconds,
        "youtube_url": f"https://www.youtube.com/watch?v=AAAAAAAAAAA&t={int(seconds)}s",
        "scoreboard": {
            "status": "observed",
            "cards": [
                {"color": color, "end_number": end, "cumulative_points": points}
                for color, cards in (("red", red), ("yellow", yellow))
                for end, points in cards
            ],
            "unreadable_slots": [],
            "conflicting_end_numbers": [],
        },
        **changes,
    }


def run(start, **kwargs):
    return [observation(start + i * 5, **kwargs) for i in range(3)]


def test_added_card_has_two_evidence_anchors_and_no_invented_score_or_end_time():
    readings = run(10) + run(30, red=((1, 2), (3, 4)))
    result = board_timeline(readings)
    assert len(result["changes"]) == 1
    change = result["changes"][0]
    assert change["color"] == "red" and change["change_type"] == "cards_added"
    assert change["added_cards"] == [{"end_number": 3, "cumulative_points": 4}]
    assert change["before"]["source_seconds"] == 20 and change["after"]["source_seconds"] == 30
    assert change["bracket_seconds"] == 10
    assert change["before"]["youtube_url"].endswith("&t=20s")
    assert change["after"]["youtube_url"].endswith("&t=30s")
    assert len(change["evidence_ids"]) == 6
    assert change["score"] is None and change["event_time_seconds"] is None
    assert change["played_end_time_seconds"] is None and change["game_id"] is None
    assert change["acceptance"] == "proposed"
    assert result == board_timeline(list(reversed(readings)))


def test_empty_row_is_not_a_zero_or_reset_and_moved_cards_are_not_new_points():
    empty = board_timeline(run(0) + run(20, red=()))["changes"][0]
    assert empty["change_type"] == "no_readable_cards" and empty["score"] is None
    assert empty["removed_cards"] == [{"end_number": 1, "cumulative_points": 2}]
    moved = board_timeline(run(0) + run(20, red=((1, 3),)))["changes"][0]
    assert moved["change_type"] == "cards_repositioned"
    assert moved["moved_cards"] == [{"end_number": 1, "before_column": 2, "after_column": 3}]
    assert moved["added_cards"] == [] and moved["removed_cards"] == []


def test_unreadable_row_breaks_votes_independently_and_marks_interruption():
    middle = observation(15)
    middle["scoreboard"]["unreadable_slots"] = [{"color": "red", "cumulative_points": 3}]
    readings = run(0) + [middle] + run(20, red=((1, 2), (3, 4)))
    result = board_timeline(readings)
    event = result["changes"][0]
    assert event["continuity"] == "interrupted_readings" and event["unusable_intervening_readings"] == 1
    yellow = [e for e in result["epochs"] if e["color"] == "yellow"]
    assert len(yellow) == 1 and len(yellow[0]["evidence_ids"]) == 7
    assert board_timeline([observation(0), observation(5), middle, observation(20)])["changes"] == []


def test_sparse_gaps_remain_wide_and_never_join_weak_runs():
    change = board_timeline(run(0) + run(600, red=((1, 2), (3, 4))))["changes"][0]
    assert change["continuity"] == "sparse_gap" and change["bracket_seconds"] == 590
    result = board_timeline([observation(0), observation(100), observation(200)])
    assert result["epochs"] == []


def test_duplicate_times_repeated_evidence_and_conflicting_reads_do_not_inflate_votes():
    first = observation(0)
    copies = [first, {**first, "id": "alt", "evidence_id": "alt-frame"}, observation(5)]
    assert board_timeline(copies)["epochs"] == []
    repeated = [observation(0), observation(5, evidence_id="frame-0"), observation(10)]
    assert board_timeline(repeated)["epochs"] == []
    conflict = observation(5, red=((1, 3),), id="conflicting-read")
    result = board_timeline(run(0) + [conflict])
    assert [e["color"] for e in result["epochs"]] == ["yellow"]
    fast = [observation(0), observation(0.25), observation(0.5)]
    assert board_timeline(fast)["epochs"] == []


@pytest.mark.parametrize(
    "field,value", [("source_id", "other"), ("source_metadata_sha256", "other"), ("model_version", "other")]
)
def test_source_manifest_and_model_boundaries_are_never_compared(field, value):
    result = board_timeline(run(0) + run(20, red=((1, 2), (3, 4)), **{field: value}))
    assert not result["changes"]


def test_missing_pins_conflicts_and_missing_layout_do_not_generate_epochs():
    for change in (
        {"source_metadata_sha256": None},
        {"model_version": None},
        {"scoreboard": {"status": "unconfigured", "cards": []}},
    ):
        assert not board_timeline(run(0, **change))["epochs"]
    readings = run(0)
    for o in readings:
        o["scoreboard"]["conflicting_end_numbers"] = [1, 2]
    assert not board_timeline(readings)["epochs"]
    wrong = copy.deepcopy(readings)
    wrong[0]["source_seconds"] = float("nan")
    assert not board_timeline(wrong)["changes"]
    with pytest.raises(ValueError):
        board_timeline([], minimum_frames=2)


def test_release_verifies_epoch_observations_and_exact_change_anchors():
    from hogline.analytics.lineage import validate_references

    readings = run(0) + run(20, red=((1, 2), (3, 4)))
    payload = {
        "sources": [{"id": "source-1"}],
        "games": [],
        "analysis_windows": [],
        "processing_scans": [],
        "observations": [],
        "source_boundary_links": [],
        "evidence": [{"id": o["evidence_id"], "source_id": "source-1"} for o in readings],
        "broadcast": {"observations": readings, "board_timeline": board_timeline(readings)},
    }
    validate_references(payload)
    altered = copy.deepcopy(payload)
    change = altered["broadcast"]["board_timeline"]["changes"][0]
    change["after"] = {**change["after"], "source_seconds": change["after"]["source_seconds"] + 1}
    with pytest.raises(ValueError, match="anchors disagree"):
        validate_references(altered)
    altered = copy.deepcopy(payload)
    altered["broadcast"]["board_timeline"]["epochs"][0]["observation_ids"].append("missing")
    with pytest.raises(ValueError, match="missing data"):
        validate_references(altered)
