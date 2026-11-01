import pytest

from hogline.reconstruction.ending_windows import ending_windows


def book(identifier, boundary=None, start=0):
    return {
        "id": identifier,
        "first_seconds": start,
        "ends": [{"official_number": 1}],
        "after_reset_epoch": {"first_seconds": boundary, "evidence_ids": ["reset-frame"]}
        if boundary
        else None,
    }


def test_internal_resets_and_tail_share_bounded_budget_and_keep_provenance():
    report = {"scorecards": [book("first", 1000), book("second", start=1100)]}
    result = ending_windows(report, 2000, 4)
    assert [w["history_id"] for w in result["windows"]] == ["first", "second", "first", "second"]
    assert result["deferred_windows"] == 12
    assert result["windows"][0]["evidence_ids"] == ["reset-frame"]
    assert result["windows"][1]["reason"] == "recording_tail"
    assert all(0 <= w["start_seconds"] < w["end_seconds"] <= 2000 for w in result["windows"])
    assert all(w["end_seconds"] - w["start_seconds"] <= 60 for w in result["windows"])


def test_short_histories_and_empty_footage_do_not_create_unbounded_windows():
    result = ending_windows({"scorecards": [book("short", 35, start=10)]}, 50, 8)
    assert [(w["start_seconds"], w["end_seconds"]) for w in result["windows"]] == [(10, 35)]
    assert ending_windows({"scorecards": []}, 50, 8)["windows"] == []
    with pytest.raises(ValueError):
        ending_windows({}, float("inf"), 8)


def test_reset_context_includes_delayed_reset_without_asserting_a_game_end():
    # March 8 Sheet 3: last delivery near 7015, board reset only several minutes later.
    result = ending_windows({"scorecards": [book("sheet3", 7440)]}, 8046, 8)
    assert any(w["start_seconds"] <= 7015 < w["end_seconds"] for w in result["windows"])
    assert "do not prove" in result["notice"]
