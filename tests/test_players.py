import copy
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from hogline.adapters.sources import digest
from hogline.analytics.actors import checked_actor_result
from hogline.analytics.exports import snapshot
from hogline.db import ActorIdentity, PlayerGroup, PlayerRevision, WindowActors
from hogline.domain.catalog import Conflict
from hogline.domain.players import (
    GroupEdit,
    IdentityMove,
    edit_group,
    move_fragment,
    players_in_session,
    register_fragments,
    same_original_track,
    sync_players,
)
from hogline.orchestration import windows, worker
from hogline.review.app import create_app
from tests.test_actors import fake_people, fake_window  # noqa: F401


@pytest.fixture
def player_result(db, sources, fake_window, fake_people):  # noqa: F811
    windows.enqueue_window(
        db, windows.WindowRequest(source_id=sources[0]["id"], start_seconds=100, end_seconds=105)
    )
    assert worker.run_one(db)["state"] == "succeeded"
    with db.snapshot_session() as session:
        return checked_actor_result(session.scalar(select(WindowActors)))


def groups(db, history=False):
    with db.snapshot_session() as session:
        return players_in_session(session, history=history)


def clone_result(db, original, **changes):
    document = copy.deepcopy(original)
    document.pop("id")
    document.pop("sha256")
    document.update(changes)
    with db.snapshot_session(write=True) as session:
        row = WindowActors(
            id=str(uuid4()),
            window_id=original["window_id"],
            version=str(uuid4()),
            sha256=digest(document),
            document=document,
        )
        session.add(row)
        session.flush()
        result = checked_actor_result(row)
        register_fragments(session, result)
    return result


def test_auto_anonymous_ids_are_stable_and_overlap_requires_identical_frame_evidence(db, player_result):
    initial = groups(db, True)
    assert len(initial["groups"]) == 2
    assert all(g["name"] is None and len(g["members"]) == 1 for g in initial["groups"])
    sync_players(db)
    assert groups(db, True) == initial
    clone_result(db, player_result)
    assert len(groups(db)["groups"]) == 2
    assert all(len(g["members"]) == 2 for g in groups(db)["groups"])
    assert (
        sum(
            m["basis"] == "identical_original_frame_overlap"
            for g in groups(db)["groups"]
            for m in g["members"]
        )
        == 2
    )
    changed = copy.deepcopy(player_result["tracks"])
    for t in changed:
        for p in t["points"]:
            p["decoded_rgb_sha256"] = "different-frame"
    clone_result(db, player_result, tracks=changed)
    assert len(groups(db)["groups"]) == 4


def test_overlap_does_not_use_faces_clothing_gaps_or_partial_agreement(player_result):
    track = player_result["tracks"][0]
    assert same_original_track(track, copy.deepcopy(track))
    for change in ("missing-hash", "one-wrong-box", "other-view", "two-samples"):
        other = copy.deepcopy(track)
        if change == "missing-hash":
            for p in other["points"]:
                p.pop("decoded_rgb_sha256")
        elif change == "one-wrong-box":
            other["points"][2]["bbox_px"][0] += 1
        elif change == "other-view":
            other["view"] = "other-view"
        else:
            other["points"] = other["points"][:2]
        assert not same_original_track(track, other)


def test_optional_name_move_split_and_move_back_are_revisioned_and_idempotent(db, player_result):
    source, target = groups(db)["groups"]
    renamed = edit_group(
        db,
        source["id"],
        GroupEdit(
            expected_revision=source["revision"],
            name="  Alex  ",
            author="Operator",
            reason="Optional user-supplied name",
        ),
    )
    assert renamed["name"] == "Alex" and renamed["id"] == source["id"]
    with pytest.raises(Conflict):
        edit_group(
            db,
            source["id"],
            GroupEdit(expected_revision=source["revision"], name="Stale", author="O", reason="Test"),
        )
    source = next(g for g in groups(db)["groups"] if g["id"] == source["id"])
    member = source["members"][0]
    request = IdentityMove(
        expected_revision=member["revision"],
        expected_source_revision=source["revision"],
        target_group_id=target["id"],
        expected_target_revision=target["revision"],
        author="Operator",
        reason="Reviewed body continuity in original video",
    )
    moved = move_fragment(db, member["id"], request)
    assert moved["group_id"] == target["id"] and moved["acceptance"] == "human_confirmed"
    with pytest.raises(Conflict):
        move_fragment(db, member["id"], request)
    sync_players(db)
    source, target = [
        next(g for g in groups(db)["groups"] if g["id"] == i) for i in (source["id"], target["id"])
    ]
    assert source["name"] == "Alex" and not source["members"] and len(target["members"]) == 2
    restored = move_fragment(
        db,
        member["id"],
        IdentityMove(
            expected_revision=moved["revision"],
            expected_source_revision=target["revision"],
            target_group_id=source["id"],
            expected_target_revision=source["revision"],
            author="Operator",
            reason="Undo mistaken link",
        ),
    )
    source = next(g for g in groups(db)["groups"] if g["id"] == source["id"])
    detached = move_fragment(
        db,
        member["id"],
        IdentityMove(
            expected_revision=restored["revision"],
            expected_source_revision=source["revision"],
            author="Operator",
            reason="Separate uncertain participant",
        ),
    )
    assert detached["group_id"] not in (source["id"], target["id"])
    history = groups(db, True)["history"]
    assert any(
        r["reason"] == "Undo mistaken link"
        and r["before"]["group_id"] == target["id"]
        and r["after"]["group_id"] == source["id"]
        for r in history
        if r["kind"] == "assignment"
    )


def test_players_api_thumbnail_exact_timestamp_release_and_corruption(db, player_result):
    client = TestClient(create_app(db.settings))
    catalog = client.get("/api/players").json()
    member = catalog["groups"][0]["members"][0]
    response = client.get(f"/api/players/fragments/{member['id']}/thumbnail")
    assert response.status_code == 200 and response.headers["content-type"] == "image/jpeg"
    assert response.content[:2] == b"\xff\xd8"
    assert client.get("/api/players/fragments/missing/thumbnail").status_code == 404
    assert client.post("/api/players/sync?limit=101").status_code == 422
    assert client.get("/api/players/history.json").json()["history"]
    assert snapshot(db, "exploratory")["players"]["groups"] == catalog["groups"]
    with db.session() as session:
        row = session.get(ActorIdentity, member["id"])
        row.group_id = catalog["groups"][1]["id"]
    with pytest.raises(ValueError, match="references disagree"):
        snapshot(db, "exploratory")


def test_history_and_group_corruption_fail_closed(db, player_result):
    with db.session() as session:
        row = session.scalar(select(PlayerRevision))
        row.sha256 = "corrupt"
    with pytest.raises(ValueError, match="revision checksum"):
        groups(db, True)
    with db.session() as session:
        row = session.scalar(select(PlayerGroup))
        row.document = {**row.document, "name": "Corrupt"}
    with pytest.raises(ValueError, match="checksum"):
        groups(db)
