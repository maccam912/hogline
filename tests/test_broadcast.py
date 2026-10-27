from datetime import UTC, datetime, timedelta

from hogline.media.clocks import fit_clock_hypotheses, parse_overlay
from hogline.perception.scoreboard import persistent_board_claims, score_deltas


def test_clock_preserves_am_pm_and_dst_alternatives():
    ordinary = parse_overlay("03/17/2026", "09:57:48")
    assert ordinary["local_date"] == "2026-03-17"
    assert len(ordinary["candidates"]) == 2
    assert len(parse_overlay("11/01/2026", "01:30:00 AM")["candidates"]) == 2
    assert parse_overlay("03/08/2026", "02:30:00 AM")["missing_reason"] == "nonexistent_local_time"
    noon = parse_overlay("01/16/2026", "12:00:00 PM")
    assert noon["candidates"][0]["local"].startswith("2026-01-16T12:")
    assert not parse_overlay("broken", "12:48:38")["candidates"]
    assert parse_overlay("02/30/2026", "14:00:00")["missing_reason"] == "invalid_overlay_clock"


def anchor(index, offset=0, seconds=None):
    seconds = index * 10 if seconds is None else seconds
    utc = datetime(2026, 3, 17, 15, tzinfo=UTC) + timedelta(seconds=seconds + offset)
    return {
        "source_id": "source",
        "source_seconds": seconds,
        "evidence_id": str(index),
        "clock": {"candidates": [{"utc": utc.isoformat()}]},
    }


def test_clock_segments_do_not_bridge_jumps_gaps_or_unreadable_clocks():
    frames = [anchor(i) for i in range(3)] + [anchor(i, 120) for i in range(3, 6)]
    frames += [anchor(i) for i in range(6, 9)]
    segments = fit_clock_hypotheses(frames)
    assert [(s["source_start_seconds"], s["source_end_seconds"]) for s in segments] == [
        (0, 20),
        (30, 50),
        (60, 80),
    ]
    assert not fit_clock_hypotheses([anchor(i, seconds=i * 600) for i in range(3)])
    assert not fit_clock_hypotheses([anchor(0)] * 5)
    frames = [anchor(0), {**anchor(1), "clock": {"candidates": []}}, anchor(2)]
    assert not fit_clock_hypotheses(frames)


CARDS = [
    {"color": "red", "end_number": 2, "cumulative_points": 3},
    {"color": "red", "end_number": 5, "cumulative_points": 5},
    {"color": "yellow", "end_number": 4, "cumulative_points": 2},
]


def board(index, seconds=None, cards=None):
    return {
        "source_id": "s",
        "evidence_id": str(index),
        "source_seconds": index * 10 if seconds is None else seconds,
        "youtube_url": f"https://www.youtube.com/watch?v=AAAAAAAAAAA&t={index * 10}s",
        "scoreboard": {
            "cards": CARDS if cards is None else cards,
            "unreadable_slots": [],
            "conflicting_end_numbers": [],
        },
    }


def test_traditional_board_subtracts_cumulative_points_without_inventing_blank_ends():
    claims = score_deltas(CARDS)
    assert len(claims) == 1
    assert claims[0]["official_number"] == 5 and claims[0]["points"] == 2
    assert claims[0]["previous_scoring_end"] == 2
    assert not any(c["official_number"] in (1, 2, 3, 4) for c in claims)


def test_board_persistence_is_temporal_not_a_vote_across_entire_day():
    assert len(persistent_board_claims([board(i) for i in range(3)])) == 1
    assert not persistent_board_claims([board(0)] * 5)
    assert not persistent_board_claims([board(i, seconds=i * 3600) for i in range(3)])
    assert not persistent_board_claims([board(0), board(1, cards=[]), board(2)])
    invalid = board(1)
    invalid["scoreboard"]["unreadable_slots"] = [{"color": "red", "cumulative_points": 5}]
    assert not persistent_board_claims([board(0), invalid, board(2)])


def test_unreadable_opponent_row_does_not_erase_a_readable_row():
    frames = [board(i) for i in range(3)]
    for frame in frames:
        frame["scoreboard"]["unreadable_slots"] = [{"color": "yellow", "cumulative_points": 2}]
    claims = persistent_board_claims(frames)
    assert len(claims) == 1 and claims[0]["color"] == "red"
