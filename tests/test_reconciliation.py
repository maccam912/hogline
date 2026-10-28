import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from hogline.analytics.exports import snapshot
from hogline.db import Evidence, GameRow, Reconciliation, ReviewTask, Source
from hogline.domain.catalog import Conflict, catalog, save_game
from hogline.domain.models import End, Game, GameEdit, Result, Span
from hogline.domain.reconciliation import (
    ReconcileCommit,
    ReconcileRequest,
    UndoReconciliation,
    commit_reconciliation,
    preview_reconciliation,
    undo_reconciliation,
)
from hogline.review.app import create_app


def game(db, sources, start=100, end=1000, **kwargs):
    obj = Game(
        sheet=5, spans=[Span(source_id=sources[0]["id"], start_seconds=start, end_seconds=end)], **kwargs
    )
    return save_game(
        db,
        GameEdit(expected_revision=0, author="Fixture reviewer", reason="Original interpretation", game=obj),
    )


def frame(db, source, seconds):
    with db.session() as s:
        obj = Evidence(
            source_id=source["id"],
            evidence_key=f"{source['id']}:{seconds}",
            document={"source_seconds": seconds},
        )
        s.add(obj)
        s.flush()
        return obj.id


def end(evidence, number=1, points=2):
    return End(
        observed_index=number,
        official_number=number,
        state="played",
        red_points=points,
        yellow_points=0,
        acceptance="human_confirmed",
        evidence_ids=[evidence],
    )


def request(action, *games, **kwargs):
    return ReconcileRequest(
        action=action,
        parents=[{"game_id": g["id"], "expected_revision": g["revision"]} for g in games],
        **kwargs,
    )


def publish(db, req):
    preview = preview_reconciliation(db, req)
    return commit_reconciliation(
        db,
        ReconcileCommit(
            request=req,
            preview_sha256=preview["preview_sha256"],
            author="Fixture reviewer",
            reason="Checked source boundaries",
        ),
    )


def test_split_is_atomic_preserves_end_history_and_can_undo(db, sources):
    a, b = frame(db, sources[0], 200), frame(db, sources[0], 800)
    original = game(db, sources, ends=[end(a), end(b, 2)])
    before = snapshot(db, "exploratory")
    req = request("split", original, source_cuts={sources[0]["id"]: 500})
    preview = preview_reconciliation(db, req)
    assert not preview["conflicts"]
    assert [g["spans"][0]["end_seconds"] for g in preview["successors"]] == [500, 1000]
    assert [g["ends"][0]["official_number"] for g in preview["successors"]] == [1, 2]
    assert all(g["ends"][0]["acceptance"] == "proposed" for g in preview["successors"])
    assert len(catalog(db)["games"]) == 1  # Preview is read-only.
    operation = publish(db, req)
    assert (
        commit_reconciliation(
            db,
            ReconcileCommit(
                request=req,
                preview_sha256=preview["preview_sha256"],
                author="Fixture reviewer",
                reason="Retry",
            ),
        )["id"]
        == operation["id"]
    )  # A network retry cannot create another split.
    data = catalog(db)
    assert len(data["games"]) == 2 and len(data["retired_games"]) == 1
    assert data["retired_games"][0]["ends"][0]["acceptance"] == "human_confirmed"
    with db.session() as s:
        assert all(
            t.state == "resolved"
            for t in s.scalars(select(ReviewTask).where(ReviewTask.game_id == original["id"]))
        )
    release = snapshot(db, "exploratory")
    assert len(release["games"]) == 2 and release["reconciliations"][0]["end_mappings"]
    assert len(before["games"]) == 1 and before["games"][0]["revision"] == 1
    undone = undo_reconciliation(
        db,
        operation["id"],
        UndoReconciliation(
            expected_revision=1, author="Fixture reviewer", reason="Restore original grouping"
        ),
    )
    assert undone["state"] == "undone" and undone["revision"] == 2
    data = catalog(db)
    assert [g["id"] for g in data["games"]] == [original["id"]]
    assert data["games"][0]["revision"] == 3 and data["games"][0]["ends"] == original["ends"]
    assert len(data["retired_games"]) == 2


def test_split_unknown_end_requires_assignment_and_crossing_evidence_blocks(db, sources):
    original = game(db, sources, ends=[End(observed_index=1)])
    req = request("split", original, source_cuts={sources[0]["id"]: 500})
    preview = preview_reconciliation(db, req)
    assert preview["preview_sha256"] is None
    assert preview["conflicts"][0]["options"] == ["earlier", "later"]
    req.end_choices = {original["ends"][0]["id"]: "later"}
    assert preview_reconciliation(db, req)["successors"][1]["ends"][0]["state"] == "unknown"
    e = end(frame(db, sources[0], 200))
    e.evidence_ids.append(frame(db, sources[0], 800))
    other = game(db, sources, ends=[e])
    preview = preview_reconciliation(db, request("split", other, source_cuts={sources[0]["id"]: 500}))
    assert preview["conflicts"][0]["options"] == []


def test_multiple_recordings_require_explicit_cuts_and_preserve_every_span(db, sources):
    original = game(db, sources)
    edited = Game.model_validate({k: v for k, v in original.items() if k != "revision"})
    edited.spans.append(
        Span(source_id=sources[1]["id"], start_seconds=0, end_seconds=300, relationship="continuation")
    )
    original = save_game(
        db, GameEdit(expected_revision=1, author="Reviewer", reason="Linked fragment", game=edited)
    )
    with pytest.raises(ValueError, match="every linked"):
        preview_reconciliation(db, request("split", original, source_cuts={sources[0]["id"]: 500}))
    plan = preview_reconciliation(
        db, request("split", original, source_cuts={sources[0]["id"]: 500, sources[1]["id"]: 0})
    )
    assert len(plan["successors"][0]["spans"]) == 1 and len(plan["successors"][1]["spans"]) == 2
    assert plan["successors"][1]["spans"][1]["relationship"] == "continuation"


def test_merge_requires_duplicate_end_choice_and_invalidates_results(db, sources):
    a, b = frame(db, sources[0], 200), frame(db, sources[0], 800)
    first = game(
        db,
        sources,
        end=500,
        ends=[end(a)],
        acceptance="human_confirmed",
        completion="complete",
        result=Result(
            outcome="red_win", red_points=2, yellow_points=0, evidence_ids=[a], acceptance="human_confirmed"
        ),
    )
    second = game(db, sources, start=700, ends=[end(b, points=3)])
    req = request("merge", first, second)
    plan = preview_reconciliation(db, req)
    assert plan["conflicts"][0]["kind"] == "duplicate_official_end"
    req.end_choices = {"1": f"{second['id']}/{second['ends'][0]['id']}"}
    plan = preview_reconciliation(db, req)
    merged = plan["successors"][0]
    assert len(merged["spans"]) == 2  # The missing 500–700 interval was not invented.
    assert merged["ends"][0]["red_points"] == 3 and merged["ends"][0]["acceptance"] == "proposed"
    assert merged["result"]["outcome"] == "unknown" and merged["completion"] == "unknown"
    assert plan["invalidated_results"] == [first["id"]]
    assert [m["action"] for m in plan["end_mappings"]] == ["superseded", "moved"]
    publish(db, req)
    assert snapshot(db)["games"] == []  # New grouping cannot inherit final-result eligibility.
    assert len(catalog(db)["retired_games"]) == 2


def test_stale_parent_source_change_or_preview_hash_cannot_publish(db, sources):
    original = game(db, sources)
    req = request("split", original, source_cuts={sources[0]["id"]: 500})
    plan = preview_reconciliation(db, req)
    command = ReconcileCommit(
        request=req, preview_sha256=plan["preview_sha256"], author="Reviewer", reason="Split"
    )
    with db.session() as s:
        source = s.get(Source, sources[0]["id"])
        source.document = {**source.document, "metadata_sha256": "changed"}
    with pytest.raises(Conflict, match="preview changed"):
        commit_reconciliation(db, command)
    assert len(catalog(db)["games"]) == 1
    with db.session() as s:
        row = s.get(GameRow, original["id"])
        row.document = {**row.document, "label": "Edited elsewhere"}
    with pytest.raises(Conflict, match="changed"):
        preview_reconciliation(db, req)
    with db.session() as s:
        assert list(s.scalars(select(Reconciliation))) == []


def test_undo_refuses_later_edits_and_ordinary_edit_cannot_retire(db, sources):
    original = game(db, sources)
    direct = Game.model_validate({k: v for k, v in original.items() if k != "revision"})
    direct.retired_into = ["missing"]
    with pytest.raises(ValueError, match="reconciliation"):
        save_game(db, GameEdit(expected_revision=1, author="Reviewer", reason="Hide", game=direct))
    operation = publish(db, request("split", original, source_cuts={sources[0]["id"]: 500}))
    child = Game.model_validate(operation["successors"][0])
    child.label = "Later human edit"
    save_game(db, GameEdit(expected_revision=1, author="Reviewer", reason="Later edit", game=child))
    with pytest.raises(Conflict, match="later work"):
        undo_reconciliation(
            db, operation["id"], UndoReconciliation(expected_revision=1, author="Reviewer", reason="Undo")
        )
    assert len(catalog(db)["games"]) == 2


def test_api_preview_publish_and_undo_require_current_revision(db, sources):
    original = game(db, sources)
    req = request("split", original, source_cuts={sources[0]["id"]: 500})
    with TestClient(create_app(db.settings)) as client:
        plan = client.post("/api/reconciliations/preview", json=req.model_dump()).json()
        payload = {
            "request": req.model_dump(),
            "preview_sha256": plan["preview_sha256"],
            "author": "Reviewer",
            "reason": "Checked grouping",
        }
        response = client.post("/api/reconciliations", json=payload)
        assert response.status_code == 200
        op = response.json()
        assert client.post("/api/reconciliations", json=payload).json()["id"] == op["id"]
        assert client.post("/api/reconciliations/preview", json=req.model_dump()).status_code == 409
        wrong = client.post(
            f"/api/reconciliations/{op['id']}/undo",
            json={"expected_revision": 2, "author": "Reviewer", "reason": "Wrong revision"},
        )
        assert wrong.status_code == 409
        assert (
            client.post(
                f"/api/reconciliations/{op['id']}/undo",
                json={"expected_revision": 1, "author": "Reviewer", "reason": "Restore grouping"},
            ).status_code
            == 200
        )


def test_failure_after_child_creation_rolls_back_entire_correction(db, sources, monkeypatch):
    from hogline.domain import reconciliation

    original = game(db, sources)
    req = request("split", original, source_cuts={sources[0]["id"]: 500})
    real_save = reconciliation.save_in_session

    def fail_on_retirement(session, edit, *args, **kwargs):
        if edit.game.retired_into:
            raise ValueError("Simulated publication failure")
        return real_save(session, edit, *args, **kwargs)

    monkeypatch.setattr(reconciliation, "save_in_session", fail_on_retirement)
    with pytest.raises(ValueError, match="Simulated"):
        publish(db, req)
    assert [g["id"] for g in catalog(db)["games"]] == [original["id"]]
    assert catalog(db)["retired_games"] == [] and catalog(db)["reconciliations"] == []


def test_merge_rejects_different_sheets(db, sources):
    first = game(db, sources)
    with db.session() as session:
        row = session.get(Source, sources[1]["id"])
        row.document = {**row.document, "sheet": 6}
    second = save_game(
        db,
        GameEdit(
            expected_revision=0,
            author="Reviewer",
            reason="Other sheet",
            game=Game(sheet=6, spans=[Span(source_id=sources[1]["id"], start_seconds=0, end_seconds=500)]),
        ),
    )
    with pytest.raises(ValueError, match="same sheet"):
        preview_reconciliation(db, request("merge", first, second))
