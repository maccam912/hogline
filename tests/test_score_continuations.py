from copy import deepcopy

from hogline.reconstruction.continuations import continuation_score_groups


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
