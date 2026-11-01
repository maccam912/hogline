"""Manual face grouping preserves observations, optional names, constraints and undo."""

from copy import deepcopy

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from hogline.adapters.sources import digest
from hogline.db import Database, GameLineup, LineupGrouping, LineupRevision
from hogline.review.app import create_app
from tests.test_lineups import fake_lineup, make_lineup  # noqa: F401


def group(client, ids, fingerprint=None):
    return client.post(
        "/api/faces/groupings",
        json={
            "player_ids": ids,
            "expected_fingerprint": fingerprint or client.get("/api/faces").json()["fingerprint"],
        },
    )


@pytest.mark.usefixtures("fake_lineup")
def test_same_game_faces_group_without_a_name_and_undo_after_restart(db, sources):
    _, lineup = make_lineup(db, sources)
    original = deepcopy(lineup)
    ids = [p["id"] for p in lineup["players"][:2]]
    client = TestClient(create_app(db.settings))
    before = client.get("/api/faces").json()
    grouped = group(client, ids)
    assert grouped.status_code == 200, grouped.text
    gid = grouped.json()["id"]
    gallery = client.get("/api/faces").json()
    assert gallery["total"] == before["total"] - 1
    # Both old URLs resolve to the same person, with all observations retained.
    details = [client.get(f"/api/faces/players/{pid}").json() for pid in ids]
    assert details[0]["id"] == details[1]["id"]
    assert details[0]["person"] is None and details[0]["games"] == 1
    assert {a["id"] for a in details[0]["appearances"]} == set(ids)
    assert details[0]["faces"] == sum(p["faces"] for p in original["players"][:2])
    assert details[0]["groupings"][0]["id"] == gid
    view = client.get(f"/api/lineups/{lineup['id']}").json()
    assert len({p["group_id"] for p in view["lineup"]["players"] if p["id"] in ids}) == 1
    assert client.post(f"/api/lineups/{lineup['id']}/regroup", json={}).status_code == 409
    assert (
        client.post(
            f"/api/faces/players/{ids[0]}/separate",
            json={
                "from_player_id": ids[1],
            },
        ).status_code
        == 409
    )
    with db.snapshot_session() as session:
        assert session.get(GameLineup, lineup["id"]).document == original
    # No process-local state is required for persistence or undo.
    restarted = Database(db.settings)
    from hogline.domain.identities import _COMPACT, _INDEX

    _COMPACT.clear()
    _INDEX.clear()
    client = TestClient(create_app(restarted.settings))
    assert client.get(f"/api/faces/players/{ids[1]}").json()["groupings"][0]["id"] == gid
    assert client.post(f"/api/faces/groupings/{gid}/delete", json={}).status_code == 200
    assert client.get("/api/faces").json()["total"] == before["total"]
    with db.snapshot_session() as session:
        history = list(session.scalars(select(LineupRevision).where(LineupRevision.kind == "grouping")))
        assert len(history) == 2 and history[-1].document["after"] is None


@pytest.mark.usefixtures("fake_lineup")
def test_grouping_tiny_faces_and_overlapping_groups_survives_deletion(db, sources):
    _, first = make_lineup(db, sources)
    _, second = make_lineup(db, sources, start_seconds=400, end_seconds=520)
    a, b = [p["id"] for p in first["players"][:2]]
    c = second["players"][2]["id"]
    # A one-face group has no template eligible for automatic linking.
    with db.snapshot_session(write=True) as session:
        row = session.get(GameLineup, first["id"])
        doc = deepcopy(row.document)
        doc["players"][0].update(faces=1, role="minor")
        row.document, row.sha256 = doc, digest(doc)
        # Operator decisions also join lineups made with different face models.
        row = session.get(GameLineup, second["id"])
        doc = deepcopy(row.document)
        doc["embedder"] = "another-embedder"
        row.document, row.sha256 = doc, digest(doc)
    client = TestClient(create_app(db.settings))
    assert group(client, [a, b]).status_code == 200
    assert group(client, [b, c]).status_code == 200
    detail = client.get(f"/api/faces/players/{a}").json()
    assert {a, b, c} <= {p["id"] for p in detail["appearances"]}
    assert len(detail["groupings"]) == 2
    assert client.post(f"/api/lineups/{second['id']}/delete", json={}).status_code == 200
    detail = client.get(f"/api/faces/players/{a}").json()
    assert {p["id"] for p in detail["appearances"]} == {a, b}
    assert len(detail["groupings"]) == 1
    assert client.post(f"/api/lineups/{first['id']}/delete", json={}).status_code == 200
    with db.snapshot_session() as session:
        assert not list(session.scalars(select(LineupGrouping)))


@pytest.mark.usefixtures("fake_lineup")
def test_grouping_rejects_stale_unknown_duplicate_separated_and_differently_named_faces(db, sources):
    _, lineup = make_lineup(db, sources)
    a, b, c, d = [p["id"] for p in lineup["players"][:4]]
    client = TestClient(create_app(db.settings))
    old = client.get("/api/faces").json()["fingerprint"]
    assert group(client, [a, a]).status_code == 422
    assert group(client, [a, "missing"]).status_code == 422
    separation = client.post(f"/api/faces/players/{a}/separate", json={"from_player_id": b}).json()
    assert group(client, [a, b], old).status_code == 409
    assert group(client, [a, b]).status_code == 409
    assert client.post(f"/api/faces/separations/{separation['id']}/delete", json={}).status_code == 200
    for pid, name in ((c, "Matt"), (d, "Someone else")):
        assert (
            client.put(
                f"/api/lineups/players/{pid}/link",
                json={
                    "new_person": {"name": name},
                },
            ).status_code
            == 200
        )
    assert group(client, [c, d]).status_code == 409
    assert group(client, [a, c]).status_code == 200
    # An individual name edit cannot silently give part of a manual group another name.
    assert (
        client.put(
            f"/api/lineups/players/{a}/link",
            json={
                "new_person": {"name": "Wrong name"},
            },
        ).status_code
        == 409
    )
    face = client.get(f"/api/faces/players/{a}").json()
    assert face["person"]["name"] == "Matt"
    seen = client.get(f"/api/faces/sources/{lineup['source_id']}").json()["games"][0]["people"]
    assert all(p["person"]["name"] == "Matt" for p in seen if p["player_id"] in {a, c})
    result = client.put(
        f"/api/faces/players/{a}/name",
        json={
            "player_ids": [a, c],
            "person_id": face["person"]["id"],
        },
    )
    assert result.status_code == 200 and result.json()["named"] == [a]
