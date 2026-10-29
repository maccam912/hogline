from copy import deepcopy

from hogline.reconstruction.continuations import continuation_score_groups, reconcile_continuation_groups
from hogline.reconstruction.scoring import reconcile_scores


def corpus():
    sources = [
        {"id": sid, "sheet": 4, "metadata_sha256": sid, "duration_seconds": duration}
        for sid, duration in (("left", 1000), ("right", 500))
    ]
    points = [(2, 0), (1, 0), (0, 0), (0, 3)]
    reports = [
        {
            "source_id": sid,
            "scorecards": [
                {
                    "id": sid + "-history",
                    "source_metadata_sha256": sid,
                    "first_seconds": first,
                    "last_seconds": last,
                    "left_censored": True,
                    "right_censored": True,
                    "ends": [
                        {
                            "official_number": n,
                            "points": {"red": r, "yellow": y},
                            "board_evidence": {"evidence_ids": [f"{sid}-{n}"]},
                        }
                        for n, (r, y) in enumerate(values, 1)
                    ],
                }
            ],
        }
        for sid, first, last, values in (("left", 0, 995, points), ("right", 0, 300, points + [(1, 0)]))
    ]
    link = {
        "id": "clock-link",
        "left_source_id": "left",
        "right_source_id": "right",
        "source_manifest_hashes": {"left": "left", "right": "right"},
        "estimated_recording_gap_seconds": 170,
        "gap_uncertainty_seconds": 4,
    }
    return sources, reports, [link]


def test_grouped_scores_deduplicate_prefix_but_preserve_original_claims_and_gap():
    sources, reports, links = corpus()
    result = continuation_score_groups(sources, reports, links)
    assert result["summary"]["source_local_known_end_scores"] == 9
    assert result["summary"]["grouped_known_end_scores"] == 5
    assert result["summary"]["physical_game_count"] is None
    group = result["groups"][0]
    assert group["source_ids"] == ["left", "right"]
    assert group["ends"][-1]["score_after_end"] == {"red": 4, "yellow": 3}
    assert len(group["ends"][2]["source_claims"]) == 2
    assert group["continuation_links"][0]["estimated_recording_gap_seconds"] == 170
    assert group["final_result"] is group["ends_played"] is None


def test_changed_manifest_reset_score_or_short_one_color_prefix_cannot_merge():
    for change in (
        "manifest",
        "book_manifest",
        "conflict",
        "disputed",
        "reset",
        "score",
        "short",
        "single_color",
        "interior",
    ):
        sources, reports, links = corpus()
        left, right = [r["scorecards"][0] for r in reports]
        if change == "manifest":
            sources[1]["metadata_sha256"] = "changed"
        elif change == "book_manifest":
            right["source_metadata_sha256"] = "obsolete"
        elif change == "conflict":
            right["card_conflicts"] = [{"official_number": 1}]
        elif change == "disputed":
            right["ends"][0]["status"] = "disputed"
        elif change == "reset":
            right["left_censored"] = False
        elif change == "score":
            right["ends"][3]["points"]["yellow"] = 4
        elif change == "short":
            left["ends"] = left["ends"][:2]
        elif change == "single_color":
            for h in (left, right):
                h["ends"][3]["points"] = {"red": 1, "yellow": 0}
        else:
            left["last_seconds"] = 100
        assert len(continuation_score_groups(sources, reports, links)["groups"]) == 2, change


def test_ambiguous_branch_and_clock_cycle_keep_histories_separate():
    sources, reports, links = corpus()
    other = deepcopy(sources[1])
    other["id"] = other["metadata_sha256"] = "third"
    sources.append(other)
    report = deepcopy(reports[1])
    report["source_id"] = "third"
    report["scorecards"][0]["id"] = "third-history"
    report["scorecards"][0]["source_metadata_sha256"] = "third"
    reports.append(report)
    extra = deepcopy(links[0])
    extra.update(
        id="other-link", right_source_id="third", source_manifest_hashes={"left": "left", "third": "third"}
    )
    assert len(continuation_score_groups(sources, reports, links + [extra])["groups"]) == 3
    sources, reports, links = corpus()
    reports[1]["scorecards"][0]["last_seconds"] = 499
    reverse = {**links[0], "id": "reverse", "left_source_id": "right", "right_source_id": "left"}
    assert len(continuation_score_groups(sources, reports, links + [reverse])["groups"]) == 2


def grouped_corpus(*, source="left", number=4, points=(0, 3), completed=True):
    sources, boards, links = corpus()
    review = {
        "id": "ice-review",
        "source_id": source,
        "request": {
            "board_history_id": source + "-history",
            "game_key": "same-game",
            "supersedes_review_ids": [],
            "official_end_number": number,
            "observed_end_index": number,
            "phase": "end_finished" if completed else "game_finished",
            "end_completed": completed,
            "red_points": points[0],
            "yellow_points": points[1],
            "final_state_evidence_ids": ["ice-frame"],
            "phase_evidence_ids": ["phase-frame"],
        },
    }
    reports = [reconcile_scores(board, [review]) for board in boards]
    return sources, continuation_score_groups(sources, boards, links), reports


def test_grouped_ice_agreement_crosses_continuation_without_duplicating_ends():
    sources, grouping, reports = grouped_corpus()
    result = reconcile_continuation_groups(grouping, reports)
    history = result["histories"][0]
    assert len(history["ends"]) == 5 and history["known_end_scores"] == 5
    end = history["ends"][3]
    assert end["comparison"] == "agreement" and end["ice_review_ids"] == ["ice-review"]
    assert {c["source_id"] for c in end["source_claims"]} == {"left", "right"}
    assert history["continuation_links"][0]["estimated_recording_gap_seconds"] == 170
    assert history["final_result"] is None and result["summary"]["physical_game_count"] is None
    import csv
    import io

    from hogline.analytics.exports import grouped_scores_csv

    evidence_ids = {f"{s}-{n}" for s in ("left", "right") for n in range(1, 6)}
    release = {
        "mode": "exploratory",
        "sources": sources,
        "grouped_score_histories": result,
        "evidence": [{"id": i} for i in evidence_ids | {"ice-frame", "phase-frame"}],
    }
    rows = list(csv.DictReader(io.StringIO(grouped_scores_csv(release))))
    assert len(rows) == 5 and rows[3]["source_ids"] == "left|right"
    assert rows[3]["ice_yellow_points"] == "3"
    assert rows[-1]["cumulative_red_points"] == "4"


def test_grouping_cannot_hide_independent_ice_disagreement_in_other_recording():
    _, grouping, reports = grouped_corpus(source="right", number=1, points=(0, 3))
    result = reconcile_continuation_groups(grouping, reports)
    history = result["histories"][0]
    assert history["ends"][0]["comparison"] == "disagreement"
    assert history["ends"][0]["points"] == {"red": None, "yellow": None}
    assert history["ends"][-1]["score_after_end"] == {"red": None, "yellow": None}
    assert history["score_history_complete_through"] is history["final_result"] is None
    assert result["summary"]["score_disagreements"] == 1


def test_unposted_lying_state_survives_grouping_without_becoming_final_score():
    _, grouping, reports = grouped_corpus(source="right", number=6, points=(0, 2), completed=None)
    history = reconcile_continuation_groups(grouping, reports)["histories"][0]
    assert len(history["ends"]) == 6 and history["score_history_complete_through"] == 5
    end = history["ends"][-1]
    assert end["lying_ice_points"] == {"red": 0, "yellow": 2} and end["ice_points"] is None
    assert end["status"] == "unresolved" and history["final_result"] is None


def test_grouped_unscored_concession_retains_the_previous_score_and_conflicts_with_posting():
    sources, reports, links = corpus()
    grouping = continuation_score_groups(sources, reports, links)
    review = {
        "id": "concession",
        "source_id": "right",
        "request": {
            "game_key": "game",
            "board_history_id": "right-history",
            "official_end_number": 6,
            "observed_end_index": 6,
            "supersedes_review_ids": [],
            "end_completed": False,
            "phase": "game_finished",
            "red_points": 2,
            "yellow_points": 0,
            "concession": {
                "rule": "world-curling-2025-R12h",
                "conceding_color": "yellow",
                "red_remaining": 0,
                "yellow_remaining": 1,
            },
            "final_state_evidence_ids": [],
            "phase_evidence_ids": [],
        },
        "evidence_refs": {},
    }
    combined = [reconcile_scores(r, [review]) for r in reports]
    result = reconcile_continuation_groups(grouping, combined)
    history = result["histories"][0]
    assert history["final_result"] == {"red": 4, "yellow": 3}
    assert history["known_end_scores"] == history["score_history_complete_through"] == 5
    assert history["ends_played"] is None
    assert history["ends"][-1]["state"] == "not_scored_concession"
    assert history["ends"][-1]["points"] == {"red": None, "yellow": None}
    other = deepcopy(combined[0]["histories"][0]["ends"][-1])
    other.update(
        official_number=6,
        observed_index=6,
        points={"red": 0, "yellow": 0},
        board_points={"red": 0, "yellow": 0},
    )
    combined[0]["histories"][0]["ends"].append(other)
    result = reconcile_continuation_groups(grouping, combined)
    assert result["histories"][0]["ends"][-1]["status"] == "disputed"
    assert result["histories"][0]["final_result"] is None
