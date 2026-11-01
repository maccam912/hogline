from copy import deepcopy

from hogline.perception.score_options import predict_score
from hogline.reconstruction.continuations import continuation_score_groups, reconcile_continuation_groups
from hogline.reconstruction.scoring import reconcile_scores
from tests.test_score_continuations import corpus


def test_close_stone_count_retains_range_but_missing_bodies_do_not():
    scoring = {
        "provisional_score": None,
        "reasons": ["point_count_requires_measurement"],
        "measurements": [
            {"color": color, "nearest_edge_interval_house_radii": interval}
            for color, interval in [
                ("red", [0.1, 0.2]),
                ("red", [0.3, 0.4]),
                ("red", [0.5, 0.6]),
                ("yellow", [0.55, 0.65]),
            ]
        ],
    }
    result = predict_score(scoring)
    assert result["score_options"] == [{"red": 2, "yellow": 0}, {"red": 3, "yellow": 0}]
    assert result["confidence"] == "uncertain"
    assert predict_score({**scoring, "reasons": ["foreground_obstruction"]})["score_options"] == []


def test_unknown_winning_color_retains_both_possibilities():
    result = predict_score(
        {
            "provisional_score": None,
            "reasons": ["nearest_colors_require_measurement"],
            "measurements": [
                {"color": c, "nearest_edge_interval_house_radii": [0.1, 0.2]} for c in ("red", "yellow")
            ],
        }
    )
    assert result["score_options"] == [{"red": 1, "yellow": 0}, {"red": 0, "yellow": 1}]


def test_ranges_cross_continuations_then_collapse_on_a_later_board_correction():
    sources, boards, links = corpus()
    # The last known end is a close ice estimate until the next recording posts it.
    boards[0]["scorecards"][0]["ends"] = boards[0]["scorecards"][0]["ends"][:3]
    for board in boards:
        board["scorecards"][0]["ends"][0]["points"] = {"red": 4, "yellow": 0}
        board["scorecards"][0]["ends"][1]["points"] = {"red": 0, "yellow": 1}
    review = {
        "id": "close-end",
        "source_id": "left",
        "evidence_refs": {},
        "request": {
            "board_history_id": "left-history",
            "game_key": "same-game",
            "supersedes_review_ids": [],
            "official_end_number": 4,
            "observed_end_index": 4,
            "end_completed": True,
            "phase": "game_finished",
            "red_points": 0,
            "yellow_points": 2,
            "score_policy": {
                "kind": "lower_bound",
                "alternatives": [
                    {"red": 0, "yellow": 2},
                    {"red": 0, "yellow": 3},
                ],
            },
            "final_state_evidence_ids": [],
            "phase_evidence_ids": [],
        },
    }
    first = reconcile_scores(boards[0], [review])["histories"][0]
    assert first["final_result_range"] == {"red": {"min": 4, "max": 4}, "yellow": {"min": 3, "max": 4}}
    assert first["possible_outcomes"] == ["red_win", "tie"]
    assert first["uncertain_end_numbers"] == [4]
    grouping = continuation_score_groups(sources, boards, links)
    assert len(grouping["groups"]) == 1
    grouped = reconcile_continuation_groups(grouping, [reconcile_scores(b, [review]) for b in boards])
    end = grouped["histories"][0]["ends"][3]
    assert end["points_range"]["yellow"] == {"min": 3, "max": 3}
    assert not end["score_is_estimate"]
    assert len(end["score_policy_applications"][0]["alternatives"]) == 2
    # With no board posting, keep the original range in grouped exports too.
    partial = deepcopy(boards)
    partial[1]["scorecards"][0]["ends"] = partial[1]["scorecards"][0]["ends"][:3]
    grouped = reconcile_continuation_groups(
        continuation_score_groups(sources, partial, links),
        [reconcile_scores(b, [review]) for b in partial],
    )
    assert grouped["histories"][0]["final_result_range"] == first["final_result_range"]


def test_missing_end_does_not_get_a_narrow_cumulative_range():
    report = {
        "source_id": "source",
        "scorecards": [
            {
                "id": "board",
                "left_censored": False,
                "right_censored": True,
                "ends": [
                    {"official_number": n, "points": {"red": 1, "yellow": 0}, "board_evidence": {}}
                    for n in (1, 3)
                ],
            }
        ],
    }
    history = reconcile_scores(report, [])["histories"][0]
    assert history["ends"][2]["score_after_end_range"] is None
    assert history["uncertain_end_numbers"] == [2]
