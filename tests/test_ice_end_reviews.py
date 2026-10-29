from uuid import NAMESPACE_URL, uuid5

import pytest
from PIL import Image
from sqlalchemy import select

from hogline.analytics.exports import csv_export, snapshot
from hogline.analytics.ice_reviews import save_ice_end_review, source_score_history
from hogline.analytics.lineage import validate_references
from hogline.db import Evidence, FrameObservation
from hogline.media.frames import observe
from hogline.reconstruction.continuations import continuation_score_groups, reconcile_continuation_groups
from hogline.reconstruction.scoring import reconcile_scores


def request(db, source):
    ids = []
    for t in range(4):
        document, _ = observe(
            Image.new("RGB", (160, 90), (210 + t, 220, 230)),
            {"pts": t * 10, "time_base_num": 1, "time_base_den": 10},
            source,
            db.settings.data_dir,
        )
        document["id"] = str(uuid5(NAMESPACE_URL, document["evidence_key"]))
        with db.session() as session:
            session.add(
                Evidence(
                    id=document["id"],
                    source_id=source["id"],
                    evidence_key=document["evidence_key"],
                    document=document,
                )
            )
        ids.append(document["id"])
    return {
        "source_id": source["id"],
        "game_key": "independent-game",
        "observed_end_index": 1,
        "official_end_number": 1,
        "view": "house_top",
        "red_points": 2,
        "yellow_points": 0,
        "phase": "game_finished",
        "end_completed": True,
        "final_state_evidence_ids": ids[:2],
        "phase_evidence_ids": ids[2:],
        "reviewer_kind": "assistant_visual",
        "author": "assistant",
        "note": "Final delivery settles, followed by handshake and cleanup.",
    }


def test_independent_ice_review_is_idempotent_exported_and_stays_proposed(db, sources):
    edit = request(db, sources[0])
    first = save_ice_end_review(db, edit)
    assert save_ice_end_review(db, edit)["id"] == first["id"]
    report = source_score_history(db, sources[0]["id"])
    assert report["summary"]["known_end_scores"] == 1
    book = report["histories"][0]
    assert book["final_result"] == {"red": 2, "yellow": 0}
    assert book["ends"][0]["comparison"] == "ice_only"
    assert book["acceptance"] == "proposed"
    release = snapshot(db, "exploratory")
    validate_references(release)
    grouped = release["grouped_score_histories"]["histories"]
    assert len(grouped) == 1 and grouped[0]["continuation_group_id"] is None
    assert grouped[0]["final_result"] == {"red": 2, "yellow": 0}
    grouped[0]["ends"][0]["points"]["red"] = 7
    with pytest.raises(ValueError, match="grouped scores"):
        validate_references(release)
    grouped[0]["ends"][0]["points"]["red"] = 2
    assert release["ice_end_reviews"][0]["id"] == first["id"]
    import csv
    import io

    rows = list(csv.DictReader(io.StringIO(csv_export(release))))
    end = next(r for r in rows if r["row_type"] == "score_history_end")
    assert end["ice_red_points"] == end["cumulative_red_points"] == "2"
    assert end["board_red_points"] == "" and end["score_comparison"] == "ice_only"
    assert not list(csv.DictReader(io.StringIO(csv_export(snapshot(db, "confirmed")))))
    release["ice_end_reviews"][0]["request"]["red_points"] = 7
    with pytest.raises(ValueError, match="review checksum"):
        validate_references(release)


def test_lower_count_is_an_audited_estimate_in_both_exports(db, sources):
    import csv
    import io
    import json

    from hogline.analytics.exports import grouped_scores_csv

    edit = request(db, sources[0])
    edit["score_policy"] = {
        "kind": "lower_bound",
        "alternatives": [{"red": 2, "yellow": 0}, {"red": 3, "yellow": 0}],
    }
    review = save_ice_end_review(db, edit)
    release = json.loads(json.dumps(snapshot(db, "exploratory")))
    validate_references(release)
    book = release["grouped_score_histories"]["histories"][0]
    end = book["ends"][0]
    assert book["final_result"] == {"red": 2, "yellow": 0} and book["result_is_estimate"]
    assert end["comparison"] == "lower_bound_estimate" and end["score_is_estimate"]
    assert end["ice_points"] is None
    assert end["score_policy_applications"][0]["review_id"] == review["id"]
    for data in (csv_export(release), grouped_scores_csv(release)):
        row = next(r for r in csv.DictReader(io.StringIO(data)) if r.get("official_number") == "1")
        assert row["score_is_estimate"] == "True" and row["red_points"] == "2"
        assert json.loads(row["score_policy_json"])[0]["alternatives"] == edit["score_policy"]["alternatives"]


def test_later_board_corrects_estimate_and_unique_expanded_history_keeps_review(db, sources):
    from copy import deepcopy

    edit = request(db, sources[0])
    edit["score_policy"] = {
        "kind": "lower_bound",
        "alternatives": [{"red": 2, "yellow": 0}, {"red": 3, "yellow": 0}],
    }
    review = deepcopy(save_ice_end_review(db, edit))
    old = {
        "id": "old",
        "source_id": sources[0]["id"],
        "source_metadata_sha256": sources[0]["metadata_sha256"],
        "reader_version": "reader",
        "first_seconds": 0,
        "last_seconds": 3,
        "left_censored": False,
        "right_censored": True,
        "ends": [
            {
                "official_number": 1,
                "points": {"red": None, "yellow": None},
                "board_evidence": {"evidence_ids": ["a", "b", "c"]},
            }
        ],
    }
    review["request"]["board_history_id"] = old["id"]
    review["board_history"] = old
    current = deepcopy(old)
    current.update(id="current", last_seconds=10)
    current["ends"][0]["points"] = {"red": 3, "yellow": 0}
    board = {"source_id": sources[0]["id"], "scorecards": [current]}
    result = reconcile_scores(board, [review])
    book = result["histories"][0]
    assert not result["unmatched_review_ids"]
    assert book["final_result"] == {"red": 3, "yellow": 0} and not book["result_is_estimate"]
    end = book["ends"][0]
    assert end["comparison"] == "scoreboard_corrected_estimate" and not end["score_is_estimate"]
    assert end["estimated_points"] == {"red": 2, "yellow": 0}
    assert book["review_board_history_associations"][0]["original_history_id"] == "old"
    grouped = reconcile_continuation_groups(continuation_score_groups(sources, [board], []), [result])
    assert grouped["histories"][0]["final_result"] == {"red": 3, "yellow": 0}
    assert grouped["histories"][0]["ends"][0]["comparison"] == "scoreboard_corrected_estimate"
    # Competing histories or a different manifest must never reassign a review arbitrarily.
    board["scorecards"].append({**current, "id": "ambiguous"})
    assert reconcile_scores(board, [review])["unmatched_review_ids"] == [review["id"]]
    board["scorecards"] = [{**current, "source_metadata_sha256": "different"}]
    assert reconcile_scores(board, [review])["unmatched_review_ids"] == [review["id"]]


def test_cutoff_policy_blank_preserves_unfinished_game_and_can_be_corrected(db, sources):
    edit = request(db, sources[0])
    edit.update(
        red_points=0,
        yellow_points=0,
        phase="unresolved",
        end_completed=None,
        score_policy={"kind": "recording_cutoff_blank"},
    )
    review = save_ice_end_review(db, edit)
    book = source_score_history(db, sources[0]["id"])["histories"][0]
    end = book["ends"][0]
    assert end["points"] == {"red": 0, "yellow": 0} and end["state"] == "blank"
    assert end["comparison"] == "recording_cutoff_blank" and end["score_is_estimate"]
    assert not end["end_completion_verified"] and book["game_completion"] == "unknown"
    assert book["final_result"] is None and book["ends_played"] is None
    review = {**review, "request": {**review["request"], "board_history_id": "board"}}
    board = {
        "source_id": sources[0]["id"],
        "scorecards": [
            {
                "id": "board",
                "left_censored": True,
                "right_censored": True,
                "ends": [{"official_number": 1, "points": {"red": 0, "yellow": 2}, "board_evidence": {}}],
            }
        ],
    }
    corrected = reconcile_scores(board, [review])["histories"][0]
    assert corrected["ends"][0]["points"] == {"red": 0, "yellow": 2}
    assert corrected["ends"][0]["comparison"] == "scoreboard_corrected_estimate"
    assert corrected["final_result"] is None


def test_score_policy_rejects_higher_choice_and_false_completion(db, sources):
    from hogline.analytics.ice_reviews import IceEndReview

    edit = request(db, sources[0])
    edit["score_policy"] = {
        "kind": "lower_bound",
        "alternatives": [
            {"red": 1, "yellow": 0},
            {"red": 2, "yellow": 0},
        ],
    }
    with pytest.raises(ValueError, match="minimum"):
        IceEndReview.model_validate(edit)
    edit.update(red_points=0, yellow_points=0, score_policy={"kind": "recording_cutoff_blank"})
    with pytest.raises(ValueError, match="without physical completion"):
        IceEndReview.model_validate(edit)


def test_unknown_ice_points_never_supply_zero_and_explicit_correction_keeps_history(db, sources):
    edit = request(db, sources[0])
    edit["red_points"] = edit["yellow_points"] = None
    first = save_ice_end_review(db, edit)
    report = source_score_history(db, sources[0]["id"])
    assert report["histories"][0]["ends"][0]["points"] == {"red": None, "yellow": None}
    assert report["histories"][0]["final_result"] is None
    edit = {**edit, "red_points": 2, "yellow_points": 0, "supersedes_review_ids": [first["id"]]}
    save_ice_end_review(db, edit)
    assert source_score_history(db, sources[0]["id"])["histories"][0]["final_result"] == {
        "red": 2,
        "yellow": 0,
    }
    with db.session() as s:
        assert len(list(s.scalars(select(FrameObservation)))) == 2


def test_board_and_ice_disagreement_remains_visible_without_total(db, sources):
    edit = request(db, sources[0])
    rv = save_ice_end_review(db, edit)
    rv["request"]["board_history_id"] = "board"
    report = {
        "source_id": sources[0]["id"],
        "scorecards": [
            {
                "id": "board",
                "left_censored": False,
                "right_censored": False,
                "ends": [
                    {
                        "official_number": 1,
                        "points": {"red": 0, "yellow": 3},
                        "board_evidence": {"evidence_ids": ["board-frame"]},
                    }
                ],
            }
        ],
    }
    book = reconcile_scores(report, [rv])["histories"][0]
    assert book["ends"][0]["comparison"] == "disagreement"
    assert book["ends"][0]["board_points"] == {"red": 0, "yellow": 3}
    assert book["ends"][0]["ice_points"] == {"red": 2, "yellow": 0}
    assert book["final_result"] is None


def test_assistant_cannot_replace_human_review_or_use_duplicate_frames(db, sources):
    edit = request(db, sources[0])
    edit["reviewer_kind"] = "human"
    first = save_ice_end_review(db, edit)
    with pytest.raises(ValueError, match="human decision"):
        save_ice_end_review(
            db, {**edit, "reviewer_kind": "assistant_visual", "supersedes_review_ids": [first["id"]]}
        )
    edit["final_state_evidence_ids"] *= 2
    edit["final_state_evidence_ids"] = [edit["final_state_evidence_ids"][0]] * 2
    with pytest.raises(ValueError, match="distinct"):
        save_ice_end_review(db, edit)


def test_game_stopping_does_not_establish_a_completed_scoring_end(db, sources):
    edit = request(db, sources[0])
    edit["end_completed"] = None
    save_ice_end_review(db, edit)
    history = source_score_history(db, sources[0]["id"])["histories"][0]
    assert history["ends"][0]["lying_ice_points"] == {"red": 2, "yellow": 0}
    assert history["ends"][0]["ice_points"] is None
    assert history["final_result"] is None and history["ends_played"] is None


def test_unresolved_final_end_preserves_known_prefix_and_legacy_projection(db, sources):
    edit = request(db, sources[0])
    edit.update(observed_end_index=2, official_end_number=2, end_completed=None)
    review = save_ice_end_review(db, edit)
    review = {**review, "request": {**review["request"], "board_history_id": "posted"}}
    report = {
        "source_id": sources[0]["id"],
        "scorecards": [
            {
                "id": "posted",
                "first_seconds": 0,
                "last_seconds": 3,
                "left_censored": False,
                "right_censored": False,
                "ends": [
                    {
                        "official_number": 1,
                        "points": {"red": 1, "yellow": 0},
                        "board_evidence": {"evidence_ids": []},
                    }
                ],
            }
        ],
    }
    result = reconcile_scores(report, [review])
    history = result["histories"][0]
    assert history["score_history_complete_through"] == 1
    assert history["ends"][1]["score_after_end"] == {"red": None, "yellow": None}
    assert history["final_result"] is None
    legacy = reconcile_scores(report, [review], version="board-and-reviewed-ice-scores-v1")
    assert legacy["histories"][0]["score_history_complete_through"] is None
    assert legacy["histories"][0]["ends"] == [
        {
            k: v
            for k, v in end.items()
            if k not in ("score_is_estimate", "estimated_points", "score_policy_applications")
        }
        for end in history["ends"]
    ]
    with pytest.raises(ValueError, match="Unsupported"):
        reconcile_scores(report, [review], version="future")


def test_concession_can_award_points_without_asserting_physical_completion_and_stays_reproducible(
    db, sources
):
    edit = request(db, sources[0])
    edit.update(
        end_completed=False,
        concession={
            "rule": "world-curling-2025-R12h",
            "conceding_color": "yellow",
            "red_remaining": 0,
            "yellow_remaining": 1,
        },
    )
    review = save_ice_end_review(db, edit)
    history = source_score_history(db, sources[0]["id"])["histories"][0]
    assert history["final_result"] == {"red": 2, "yellow": 0}
    assert history["ends_played"] is None and not history["ends"][0]["end_completion_verified"]
    assert history["ends"][0]["concession_decisions"][0]["reason"] == "points_required_to_determine_outcome"
    release = snapshot(db, "exploratory")
    validate_references(release)
    assert release["grouped_score_histories"]["histories"][0]["final_result"] == {"red": 2, "yellow": 0}
    report = {"source_id": sources[0]["id"], "scorecards": []}
    for version in ("board-and-reviewed-ice-scores-v1", "board-and-reviewed-ice-scores-v2"):
        assert reconcile_scores(report, [review], version=version)["histories"][0]["final_result"] is None


def test_conceded_unscored_end_keeps_prior_total_and_does_not_become_a_blank(db, sources):
    edit = request(db, sources[0])
    edit.update(
        observed_end_index=2,
        official_end_number=2,
        end_completed=False,
        red_points=0,
        yellow_points=4,
        concession={
            "rule": "world-curling-2025-R12h",
            "conceding_color": "red",
            "red_remaining": 1,
            "yellow_remaining": 0,
        },
    )
    review = save_ice_end_review(db, edit)
    review["request"]["board_history_id"] = "posted"
    report = {
        "source_id": sources[0]["id"],
        "scorecards": [
            {
                "id": "posted",
                "first_seconds": 0,
                "last_seconds": 3,
                "left_censored": False,
                "right_censored": False,
                "ends": [
                    {
                        "official_number": 1,
                        "points": {"red": 1, "yellow": 6},
                        "board_evidence": {"evidence_ids": []},
                    }
                ],
            }
        ],
    }
    history = reconcile_scores(report, [review])["histories"][0]
    end = history["ends"][-1]
    assert end["status"] == "not_scored" and end["state"] == "not_scored_concession"
    assert end["points"] == {"red": None, "yellow": None} and end["lying_ice_points"] == {
        "red": 0,
        "yellow": 4,
    }
    assert history["final_result"] == end["score_after_end"] == {"red": 1, "yellow": 6}
    assert history["known_end_scores"] == history["score_history_complete_through"] == 1


def test_concession_facts_cannot_replace_an_all_stones_completed_review(db, sources):
    edit = request(db, sources[0])
    edit.update(
        end_completed=False,
        concession={
            "rule": "world-curling-2025-R12h",
            "conceding_color": "red",
            "red_remaining": 0,
            "yellow_remaining": 0,
        },
    )
    with pytest.raises(ValueError, match="All stones played"):
        save_ice_end_review(db, edit)


def test_unscored_concession_conflicts_with_a_posted_or_completed_end(db, sources):
    from copy import deepcopy

    edit = request(db, sources[0])
    edit.update(
        end_completed=False,
        concession={
            "rule": "world-curling-2025-R12h",
            "conceding_color": "red",
            "red_remaining": 2,
            "yellow_remaining": 1,
        },
    )
    review = save_ice_end_review(db, edit)
    review["request"]["board_history_id"] = "posted"
    report = {
        "source_id": sources[0]["id"],
        "scorecards": [
            {
                "id": "posted",
                "first_seconds": 0,
                "last_seconds": 3,
                "left_censored": False,
                "right_censored": False,
                "ends": [
                    {
                        "official_number": 1,
                        "points": {"red": 0, "yellow": 0},
                        "board_evidence": {"evidence_ids": []},
                    }
                ],
            }
        ],
    }
    history = reconcile_scores(report, [review])["histories"][0]
    assert history["ends"][0]["status"] == "disputed" and history["final_result"] is None
    completed = deepcopy(review)
    completed["id"] = "independent-completed"
    completed["request"].pop("concession")
    completed["request"].update(board_history_id=None, end_completed=True)
    review["request"]["board_history_id"] = None
    history = reconcile_scores({"source_id": sources[0]["id"], "scorecards": []}, [review, completed])[
        "histories"
    ][0]
    assert history["ends"][0]["status"] == "disputed" and history["final_result"] is None


def test_concession_may_follow_release_of_the_teams_last_stone(db, sources):
    edit = request(db, sources[0])
    edit.update(
        end_completed=False,
        red_points=0,
        yellow_points=2,
        concession={
            "rule": "world-curling-2025-R12h",
            "conceding_color": "red",
            "red_remaining": 0,
            "yellow_remaining": 1,
        },
        note="Red concedes while its eighth stone is still moving; yellow has not played hammer.",
    )
    save_ice_end_review(db, edit)
    history = source_score_history(db, sources[0]["id"])["histories"][0]
    assert history["final_result"] == {"red": 0, "yellow": 2}
    assert history["ends_played"] is None
    assert history["ends"][0]["concession_decisions"][0]["reason"] == "counting_team_has_last_stone_remaining"
