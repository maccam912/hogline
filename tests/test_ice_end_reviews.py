from uuid import NAMESPACE_URL, uuid5

import pytest
from PIL import Image
from sqlalchemy import select

from hogline.analytics.exports import csv_export, snapshot
from hogline.analytics.ice_reviews import save_ice_end_review, source_score_history
from hogline.analytics.lineage import validate_references
from hogline.db import Evidence, FrameObservation
from hogline.media.frames import observe
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
    assert legacy["histories"][0]["ends"] == history["ends"]
    with pytest.raises(ValueError, match="Unsupported"):
        reconcile_scores(report, [review], version="future")
