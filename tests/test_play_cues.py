import copy

from hogline.reconstruction.play import focused_play
from tests.test_house_states import row


def test_focus_excludes_parked_border_handles_but_retains_in_house_ambiguity():
    rows = [row(i, (80,) if i < 5 else ()) for i in range(10)]
    for sample in rows:
        parked = {"color": "red", "center_px": [1, 1], "boundary_clipped": True}
        sample["detections"].append(parked)
        sample["rejected_candidates"] = [parked]
    result = focused_play(rows, "parquet")
    assert len(result["transitions"]) == 1
    assert result["transitions"][0]["kind"] == "possible_clearing"
    assert result["diagnostics"]["outside_house_detections_excluded"] == 10
    assert result["diagnostics"]["outside_house_rejections_excluded"] == 10
    assert result["arrival_candidates"] == []
    changed = copy.deepcopy(rows)
    for sample in changed:
        sample["rejected_candidates"][0]["center_px"] = [100, 100]
    assert focused_play(changed, "parquet")["states"] == []


def test_first_visible_house_handles_are_candidate_arrivals_not_counted_deliveries():
    rows = [row(i, () if i < 5 else (80,)) for i in range(10)]
    result = focused_play(rows, "parquet")
    (arrival,) = result["arrival_candidates"]
    assert arrival["first_throw"] is None and arrival["official_end_number"] is None
    assert arrival["before"]["source_seconds"] < arrival["after"]["source_seconds"]
    assert result == focused_play(list(reversed(rows)), "parquet")


def test_cross_window_brackets_require_complete_dense_coverage_and_same_inputs():
    from hogline.reconstruction.play import VERSION, bracket_play_changes

    occupied = focused_play([row(i, (80,)) for i in range(5)], "occupied")["states"][0]
    empty = focused_play([row(i, ()) for i in range(5)], "empty")["states"][0]
    empty = {
        **empty,
        "id": "empty",
        "start_seconds": 20,
        "end_seconds": 22,
        "first": {**empty["first"], "source_seconds": 20},
        "last": {**empty["last"], "source_seconds": 22},
    }
    base = {
        "source_id": "source",
        "source_metadata_sha256": "manifest",
        "profile_sha256": "profile",
        "detector_version": "detector",
        "visibility_version": "visibility",
        "version": VERSION,
        "status": "derived",
        "sha256": "hash",
    }
    left = {
        **base,
        "id": "left",
        "window_id": "w-left",
        "states": [occupied],
        "dense_coverage": {"start_seconds": 0, "last_sample_seconds": 9.5, "interval_seconds": 0.5},
    }
    right = {
        **base,
        "id": "right",
        "window_id": "w-right",
        "states": [empty],
        "dense_coverage": {"start_seconds": 10, "last_sample_seconds": 29.5, "interval_seconds": 0.5},
    }
    (bracket,) = bracket_play_changes([left, right])
    assert bracket["transitions"][0]["earliest_seconds"] == occupied["end_seconds"]
    assert bracket["transitions"][0]["latest_seconds"] == 20
    assert bracket["supporting_window_ids"] == ["w-left", "w-right"]
    gap = {**right, "dense_coverage": {**right["dense_coverage"], "start_seconds": 11}}
    assert bracket_play_changes([left, gap]) == []
    assert bracket_play_changes([left, {**right, "profile_sha256": "different"}]) == []
    # Reverse the visible occupancy change without claiming the first physical throw.
    left["states"] = [{**occupied, "candidate_count": 0}]
    right["states"] = [{**empty, "candidate_count": 1}]
    (bracket,) = bracket_play_changes([left, right])
    assert bracket["arrival_candidates"][0]["first_throw"] is None
