import copy

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from hogline.domain.catalog import Conflict
from hogline.perception.scoreboard import persistent_board_claims
from hogline.reconstruction.score_conflicts import score_conflicts
from hogline.review import score_conflicts as reviews
from hogline.review.app import create_app
from tests.test_board_events import run


def fixture():
    observations = run(20, red=((1, 2), (3, 4)))
    return {
        "sources": [{"id": "source-1", "metadata_sha256": "manifest-1"}],
        "evidence": [
            {"id": o["evidence_id"], "source_id": o["source_id"], "source_seconds": o["source_seconds"]}
            for o in observations
        ],
        "games": [
            {
                "id": "game",
                "revision": 1,
                "label": "Synthetic game",
                "sheet": 4,
                "spans": [{"source_id": "source-1", "start_seconds": 0, "end_seconds": 60}],
                "ends": [
                    {
                        "id": "end",
                        "official_number": 3,
                        "observed_index": 1,
                        "state": "played",
                        "red_points": 1,
                        "yellow_points": 0,
                        "acceptance": "human_confirmed",
                        "evidence_ids": ["frame-20"],
                    }
                ],
            }
        ],
    }, {"version": "reader-1", "score_proposals": persistent_board_claims(observations)}


def test_disagreement_preserves_scores_and_includes_both_evidence_sets():
    data, broadcast = fixture()
    before = copy.deepcopy(data)
    result = score_conflicts(data, broadcast)
    assert len(result) == 1
    conflict = result[0]
    assert conflict["saved_score"]["red_points"] == 1
    assert conflict["saved_score"]["acceptance"] == "human_confirmed"
    assert conflict["proposals"][0]["red_points"] == 2
    assert conflict["proposals"][0]["evidence_ids"] == ["frame-20", "frame-25", "frame-30"]
    assert conflict["proposals"][0]["youtube_links"][0].endswith("&t=20s")
    assert data == before
    data["games"][0]["ends"][0]["red_points"] = 2
    assert score_conflicts(data, broadcast) == []


@pytest.mark.parametrize(
    "change",
    [
        "outside",
        "unknown_number",
        "unknown_score",
        "manifest",
        "model",
        "missing_frame",
        "wrong_source",
        "retired",
    ],
)
def test_unrelated_or_incomplete_proposals_cannot_dispute_a_saved_score(change):
    data, broadcast = fixture()
    game = data["games"][0]
    end = game["ends"][0]
    proposal = broadcast["score_proposals"][0]
    if change == "outside":
        game["spans"][0]["end_seconds"] = 29
    if change == "unknown_number":
        end["official_number"] = None
    if change == "unknown_score":
        end["state"] = "unknown"
    if change == "manifest":
        proposal["source_metadata_sha256"] = "other"
    if change == "model":
        proposal["model_version"] = "older"
    if change == "missing_frame":
        data["evidence"].pop()
    if change == "wrong_source":
        data["evidence"][0]["source_id"] = "other"
    if change == "retired":
        game["retired_into"] = ["next"]
    assert score_conflicts(data, broadcast) == []


def test_opposing_color_blank_and_competing_proposals_remain_visible():
    data, broadcast = fixture()
    p = broadcast["score_proposals"][0]
    broadcast["score_proposals"].append({**p, "id": "matching", "points": 1})
    conflict = score_conflicts(data, broadcast)[0]
    assert len(conflict["proposals"]) == 2
    assert {p["agrees_with_saved"] for p in conflict["proposals"]} == {False, True}
    data["games"][0]["ends"][0].update(state="blank", red_points=0, yellow_points=0)
    assert len(score_conflicts(data, broadcast)) == 1
    p["color"] = "yellow"
    assert any(p["yellow_points"] == 2 for p in score_conflicts(data, broadcast)[0]["proposals"])


def test_audited_acknowledgment_reopen_and_changed_game_cannot_reuse_old_review(db, monkeypatch):
    data, broadcast = fixture()
    before = copy.deepcopy(data)
    monkeypatch.setattr(reviews, "catalog_in_session", lambda _: data)
    monkeypatch.setattr(reviews, "broadcast_catalog_in_session", lambda *_: broadcast)
    conflict = reviews.get_score_conflicts(db)["conflicts"][0]
    request = reviews.ScoreConflictDecision(
        expected_review_revision=0,
        decision="keep_saved_score",
        author="Fixture reviewer",
        reason="Synthetic review: card OCR is wrong",
    )
    saved = reviews.save_score_conflict_review(db, conflict["id"], request)
    assert saved["review_revision"] == 1
    assert data == before
    with pytest.raises(Conflict, match="reviewed elsewhere"):
        reviews.save_score_conflict_review(db, conflict["id"], request)
    acknowledged = reviews.get_score_conflicts(db)
    assert acknowledged["open_count"] == 0
    assert acknowledged["conflicts"][0]["review_state"] == "acknowledged"
    reviews.save_score_conflict_review(
        db,
        conflict["id"],
        request.model_copy(update={"expected_review_revision": 1, "decision": "needs_review"}),
    )
    assert reviews.get_score_conflicts(db)["open_count"] == 1
    data["games"][0]["revision"] = 2
    changed = reviews.get_score_conflicts(db)
    assert changed["conflicts"][0]["id"] != conflict["id"]
    assert changed["conflicts"][0]["review_revision"] == 0
    assert len(changed["reviews"]) == 2
    with pytest.raises(Conflict, match="changed or no longer exists"):
        reviews.save_score_conflict_review(db, conflict["id"], request)
    data["games"][0]["ends"][0]["red_points"] = 2
    assert reviews.get_score_conflicts(db)["conflicts"] == []
    assert len(reviews.get_score_conflicts(db)["reviews"]) == 2


def test_review_api_checks_revision_and_requires_nonblank_reason(db, monkeypatch):
    data, broadcast = fixture()
    monkeypatch.setattr(reviews, "catalog_in_session", lambda _: data)
    monkeypatch.setattr(reviews, "broadcast_catalog_in_session", lambda *_: broadcast)
    client = TestClient(create_app(db.settings))
    conflict = client.get("/api/score-disagreements").json()["conflicts"][0]
    payload = {
        "expected_review_revision": 0,
        "decision": "keep_saved_score",
        "author": "Test",
        "reason": "Reviewed fixture",
    }
    assert client.post(f"/api/score-disagreements/{conflict['id']}/reviews", json=payload).status_code == 200
    assert client.post(f"/api/score-disagreements/{conflict['id']}/reviews", json=payload).status_code == 409
    assert client.post("/api/score-disagreements/missing/reviews", json=payload).status_code == 409
    with pytest.raises(ValidationError):
        reviews.ScoreConflictDecision(**{**payload, "reason": "  "})


def test_export_recomputes_disagreements_and_checks_review_history(db, monkeypatch):
    from hogline.reconstruction.score_conflicts import validate_review_export

    data, broadcast = fixture()
    monkeypatch.setattr(reviews, "catalog_in_session", lambda _: data)
    monkeypatch.setattr(reviews, "broadcast_catalog_in_session", lambda *_: broadcast)
    current = reviews.get_score_conflicts(db)
    validate_review_export(current, data, broadcast)
    conflict = current["conflicts"][0]
    reviews.save_score_conflict_review(
        db,
        conflict["id"],
        reviews.ScoreConflictDecision(
            expected_review_revision=0, decision="keep_saved_score", author="Fixture", reason="Test review"
        ),
    )
    current = reviews.get_score_conflicts(db)
    validate_review_export(current, data, broadcast)
    for target, key, value in [
        ("conflict", "review_state", "open"),
        ("conflict", "review_revision", 4),
        ("review", "reason", "forged"),
    ]:
        forged = copy.deepcopy(current)
        record = forged["conflicts" if target == "conflict" else "reviews"][0]
        record[key] = value
        with pytest.raises(ValueError):
            validate_review_export(forged, data, broadcast)
    forged = copy.deepcopy(current)
    forged["conflicts"] = []
    with pytest.raises(ValueError):
        validate_review_export(forged, data, broadcast)
    data["games"][0]["ends"][0]["red_points"] = 2
    resolved = reviews.get_score_conflicts(db)
    assert resolved["conflicts"] == [] and len(resolved["reviews"]) == 1
    validate_review_export(resolved, data, broadcast)
