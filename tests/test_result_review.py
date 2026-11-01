import pytest
from fastapi.testclient import TestClient

from hogline.reconstruction.result_review import result_review
from hogline.review.app import create_app


def report(count=0, *, completed=False):
    book = {
        "id": "history",
        "game_completion": "complete" if completed else "unknown",
        "ends": [{"official_number": i, "points": {"red": 1, "yellow": 0}} for i in range(1, count + 1)],
    }
    return {"scorecard_reconstruction": {"scorecards": [book] if count else []}}


@pytest.mark.parametrize("ends", [6, 8, 10])
def test_posted_scores_never_establish_game_length_or_completion(ends):
    summary = result_review(report(ends))
    assert summary["status"] == "scores_extracted"
    assert summary["known_end_scores"] == ends
    assert summary["proposed_complete_games"] == 0
    assert summary["histories"][0]["scheduled_ends"] is None
    assert summary["histories"][0]["blockers"][0]["code"] == "game_ending_unreviewed"
    assert result_review(report(ends, completed=True))["status"] == "complete_results"


def test_empty_reports_partial_claims_and_missing_scores_are_distinct():
    assert result_review({})["status"] == "no_scores"
    assert result_review({"partial_score_proposals": [{}]})["status"] == "partial_scores"
    data = report(3)
    book = data["scorecard_reconstruction"]["scorecards"][0]
    book["ends"][1]["points"] = {"red": None, "yellow": None}
    book["ends"][1]["board_evidence"] = None
    book["pending_end_numbers"] = [4]
    book["card_conflicts"] = [{}]
    summary = result_review(data)
    assert summary["known_end_scores"] == 2
    blockers = summary["histories"][0]["blockers"]
    assert blockers[0]["end_numbers"] == [2, 4]
    assert [b["code"] for b in blockers] == [
        "unresolved_end_scores",
        "conflicting_board_readings",
        "game_ending_unreviewed",
    ]


def test_one_complete_game_does_not_hide_another_incomplete_game():
    data = report(6, completed=True)
    data["scorecard_reconstruction"]["scorecards"] += [
        {**report(2)["scorecard_reconstruction"]["scorecards"][0], "id": "second"}
    ]
    summary = result_review(data)
    assert summary["status"] == "scores_extracted"
    assert summary["proposed_complete_games"] == 1


def test_live_result_explains_empty_extraction_without_rewriting_saved_reports(db, sources):
    response = TestClient(create_app(db.settings)).get(f"/api/sources/{sources[0]['id']}/end-timeline")
    assert response.status_code == 200
    assert response.json()["result_review"]["status"] == "no_scores"


def test_visual_review_evidence_survives_completed_recording_cleanup(db, sources):
    from hogline.analytics.ice_reviews import save_ice_end_review
    from hogline.db import ProcessingBatch
    from hogline.media.retention import POLICY, cleanup_completed
    from tests.test_ice_end_reviews import request

    edit = request(db, sources[0])
    saved = save_ice_end_review(db, edit)
    with db.session() as session:
        session.add(
            ProcessingBatch(
                state="completed",
                document={
                    "storage_policy": POLICY,
                    "request": {"source_ids": [sources[0]["id"]]},
                },
            )
        )
    with db.session() as session:
        cleanup_completed(session, db.settings.data_dir, review_bytes=0)
    assert all(
        (db.settings.data_dir / pin["source_image"]["path"]).is_file()
        for pin in saved["evidence_refs"].values()
    )
    data = TestClient(create_app(db.settings)).get(f"/api/sources/{sources[0]['id']}/end-timeline").json()
    assert data["result_review"]["status"] == "complete_results"
    assert data["result_review"]["reviews"][0]["note"] == edit["note"]


def test_review_looks_up_boards_only_for_the_selected_recording(db, sources, monkeypatch):
    from hogline.analytics import ice_reviews
    from tests.test_ice_end_reviews import request

    calls = []

    def boards(session, root, *, source_id):
        calls.append(source_id)
        return {"observations": []}

    monkeypatch.setattr(ice_reviews, "broadcast_catalog_in_session", boards)
    edit = {**request(db, sources[0]), "board_history_id": "missing"}
    with pytest.raises(ValueError, match="Board history changed or not found"):
        ice_reviews.save_ice_end_review(db, edit)
    ice_reviews.source_score_history(db, sources[0]["id"])
    assert calls == [sources[0]["id"], sources[0]["id"]]


def test_completion_claim_cannot_hide_conflicting_or_missing_scores():
    data = report(6, completed=True)
    book = data["scorecard_reconstruction"]["scorecards"][0]
    book["card_conflicts"] = [{"reason": "competing_end_labels_in_same_cumulative_column"}]
    assert result_review(data)["status"] == "scores_extracted"
    assert result_review(data)["proposed_complete_games"] == 0
    book["card_conflicts"] = []
    book["ends"][2]["points"] = {"red": None, "yellow": None}
    assert result_review(data)["status"] == "scores_extracted"
