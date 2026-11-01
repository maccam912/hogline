"""Automatic lineups, representative faces and cross-game identities."""

from io import BytesIO

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select

from hogline.db import AutoLineup, GameLineup, ProcessingBatch
from hogline.orchestration import auto_lineups, lineups
from tests.test_automatic import media  # noqa: F401
from tests.test_lineups import DIM, PEOPLE, fake_lineup, make_lineup, run_all  # noqa: F401


def unit(v):
    v = np.asarray(v, dtype=np.float64)
    return v / np.linalg.norm(v)


def test_identity_links_respect_games_names_and_separations():
    from hogline.reconstruction.identities import link_identities

    rng = np.random.default_rng(11)
    vectors, keys = [], []
    # Person 0 in games g1-g4, person 1 in g1-g3; a look-alike of person 0 shares g2 with them.
    for game in ("g1", "g2", "g3", "g4"):
        vectors.append(unit(PEOPLE[0] + rng.normal(scale=0.05, size=DIM)))
        keys.append({game})
    for game in ("g1", "g2", "g3"):
        vectors.append(unit(PEOPLE[1] + rng.normal(scale=0.05, size=DIM)))
        keys.append({game})
    vectors.append(unit(PEOPLE[0] + rng.normal(scale=0.05, size=DIM)))  # twin, also in g2
    keys.append({"g2"})
    weights = [100] * len(vectors)
    labels = link_identities(np.array(vectors), keys, weights, 0.75)
    assert len(set(labels[:4])) == 1 and len(set(labels[4:7])) == 1 and labels[0] != labels[4]
    # The look-alike never joins person 0 in a game they shared.
    group = [i for i in range(8) if labels[i] == labels[7]]
    assert len({k for i in group for k in keys[i]}) == len(group)
    # Two named people never merge; an operator separation splits a pair.
    named = [{"g1", "named"}, {"g5", "named"}]
    assert len(set(link_identities(np.array(vectors[:2]), named, [1, 1], 0.75))) == 2
    separated = [{"g1", "separation:x"}, {"g5", "separation:x"}]
    assert len(set(link_identities(np.array(vectors[:2]), separated, [1, 1], 0.75))) == 2
    assert link_identities(np.zeros((0, DIM)), [], [], 0.75).shape == (0,)


def test_thumbnails_spread_across_people_and_portrait_is_typical():
    from hogline.reconstruction.lineups import portrait_face, select_thumbnails

    rng = np.random.default_rng(2)
    meta, vectors = [], []
    for person, eye in ((0, 20.0), (1, 8.0), (2, 7.0)):  # person 0 always stands nearest
        for n in range(30):
            meta.append({"eye_px": eye + n * 0.01, "yaw_proxy": 0.0, "sharpness": 400, "source_seconds": n})
            vectors.append(unit(PEOPLE[person] + rng.normal(scale=0.05, size=DIM)))
    kept = select_thumbnails(meta, np.array(vectors), 24, per_person=8)
    people = [i // 30 for i in kept]
    assert len(kept) == 24 and {people.count(p) for p in (0, 1, 2)} == {8}
    # Largest-only selection would have spent every slot on person 0.
    for slot, i in enumerate(sorted(kept)):
        meta[i]["thumb"] = slot
    members = list(range(30))
    meta[29]["eye_px"] = 40.0  # a large stray face of somebody else inside the group
    vectors[29] = unit(PEOPLE[3])
    meta[29]["thumb"] = 99
    index, similarity = portrait_face(members, meta, np.array(vectors), unit(np.mean(vectors[:29], axis=0)))
    assert index != 29 and similarity > 0.9


def test_game_spans_pad_scores_and_split_long_unscored_recordings():
    spans, basis = auto_lineups.game_spans(
        7200, [{"first_seconds": 1500, "last_seconds": 6000}, {"first_seconds": 5000, "last_seconds": 6900}]
    )
    assert basis == "posted_score_histories" and spans == [(300.0, 7200.0)]
    spans, basis = auto_lineups.game_spans(36000, [])
    assert basis == "whole_recording" and len(spans) == 4
    assert spans[0][0] == 0 and spans[-1][1] == 36000 and all(b - a <= 3 * 3600 for a, b in spans)


def completed_analysis(db, source_id, scorecards):
    with db.snapshot_session(write=True) as session:
        session.add(
            ProcessingBatch(
                state="completed",
                document={
                    "kind": "automatic",
                    "campaign_purpose": "recording_analysis",
                    "created_at": "2026-10-30T00:00:00+00:00",
                    "request": {"source_ids": [source_id]},
                    "units": [],
                    "analysis_result": {"scorecard_reconstruction": {"scorecards": scorecards}},
                },
            )
        )


@pytest.mark.usefixtures("fake_lineup")
def test_automatic_lineups_follow_analysis_and_backfill(db, sources, monkeypatch):
    from dataclasses import replace

    from hogline.db import Database

    monkeypatch.setattr(auto_lineups, "lineup_models", lambda root, key=None: ("fake", "fake-face-v1"))
    monkeypatch.setattr(auto_lineups, "MIN_RECORDING_SECONDS", 60)
    db = Database(replace(db.settings, auto_lineup_interval=2.0, auto_lineup_parallelism=2))
    short = sources[1]["id"]  # 7200 s
    completed_analysis(db, short, [{"first_seconds": 1300, "last_seconds": 1400}])
    with db.snapshot_session(write=True) as session:
        auto_lineups.request_in_session(session, short, "recording_analysis")
    started = auto_lineups.advance_auto_lineups(db, force=True)
    assert started == [{"source_id": short, "state": "running"}]
    with db.snapshot_session() as session:
        row = session.get(AutoLineup, short)
        batch = session.get(ProcessingBatch, row.document["batch_ids"][0])
        assert (
            row.document["spans"] == [[100.0, 2000.0]] and row.document["basis"] == "posted_score_histories"
        )
        assert batch.document["automatic"]["version"] == auto_lineups.VERSION
        assert batch.document["parallelism"] == 2 and batch.document["lineup"]["interval_seconds"] == 2.0
        unit_request = batch.document["units"][0]["payload"]["request"]
        assert unit_request["thumbnails"] == {"policy": "diverse-clear-faces-v1", "max": 96}
    # One slot: nothing else starts while this recording runs.
    long = sources[0]["id"]
    completed_analysis(db, long, [])
    assert auto_lineups.backfill(db) == {"queued": 1, "already_queued": 1, "has_lineup": 0, "not_analyzed": 0}
    assert auto_lineups.advance_auto_lineups(db, force=True) is None
    run_all(db, limit=200)
    auto_lineups.advance_auto_lineups(db, force=True)
    with db.snapshot_session() as session:
        assert session.get(AutoLineup, short).state == "done"
        lineup = session.scalars(select(GameLineup).where(GameLineup.source_id == short)).one().document
        assert session.get(AutoLineup, long).state == "running"
    assert lineup["request"]["automatic"]["basis"] == "posted_score_histories"
    assert all(p["portrait"]["thumb"] >= 0 for p in lineup["players"] if p["role"] == "player")
    status = auto_lineups.status(db)
    assert status["counts"] == {"done": 1, "running": 1} and status["enabled"]
    # Operator controls: a running recording cannot be skipped; others can be skipped and re-queued.
    with pytest.raises(ValueError):
        auto_lineups.control(db, long, "skip")
    with pytest.raises(ValueError):
        auto_lineups.control(db, short, "queue")


def test_junior_titles_and_disabled_scheduling_are_respected(db, sources, monkeypatch):
    from dataclasses import replace

    from hogline.db import Database, Source

    monkeypatch.setattr(auto_lineups, "lineup_models", lambda root, key=None: ("fake", "fake-face-v1"))
    with db.snapshot_session(write=True) as session:
        row = session.get(Source, sources[1]["id"])
        row.document = {**row.document, "title": "Junior League | 01-16-26"}
        auto_lineups.request_in_session(session, sources[1]["id"], "recording_analysis")
    off = Database(replace(db.settings, auto_lineups=False))
    assert auto_lineups.advance_auto_lineups(off, force=True) is None
    auto_lineups.advance_auto_lineups(db, force=True)
    with db.snapshot_session() as session:
        row = session.get(AutoLineup, sources[1]["id"])
        assert row.state == "skipped" and "junior" in row.document["skip_reason"]
        assert not list(session.scalars(select(ProcessingBatch)))
    # The operator can queue it after all, or skip a recording before it runs.
    assert auto_lineups.control(db, sources[1]["id"], "queue")["state"] == "waiting"
    assert auto_lineups.control(db, sources[0]["id"], "skip")["state"] == "skipped"
    assert auto_lineups.status(db)["counts"] == {"waiting": 1, "skipped": 1}


@pytest.mark.usefixtures("fake_lineup")
def test_faces_link_across_games_and_open_every_game(db, sources):
    from hogline.review.app import create_app

    _, first = make_lineup(db, sources)
    _, second = make_lineup(db, sources, start_seconds=400, end_seconds=520)
    client = TestClient(create_app(db.settings))
    gallery = client.get("/api/faces").json()
    two_game = [i for i in gallery["identities"] if i["games"] == 2]
    assert len(two_game) == 6 and gallery["counts"]["in_two_or_more_games"] == 6
    face = two_game[0]
    detail = client.get(f"/api/faces/players/{face['id']}").json()
    assert {a["lineup"]["id"] for a in detail["appearances"]} == {first["id"], second["id"]}
    assert all(a["similarity"] > 0.9 for a in detail["appearances"])
    other_player = next(a["id"] for a in detail["appearances"] if a["id"] != face["id"])
    # Selecting either game's face opens the same person.
    assert client.get(f"/api/faces/players/{other_player}").json()["id"] == face["id"]
    image = client.get(
        f"/api/lineups/{face['portrait']['lineup_id']}/players/{face['id']}/portrait.jpg?size=80"
    )
    assert image.headers["content-type"] == "image/jpeg" and Image.open(BytesIO(image.content)).size == (
        80,
        80,
    )
    # Naming the face names every game the operator saw.
    ids = [a["id"] for a in detail["appearances"]]
    named = client.put(
        f"/api/faces/players/{face['id']}/name",
        json={"player_ids": ids, "new_person": {"name": "Matt", "team": "Koski Team"}, "reason": "Named"},
    ).json()
    assert sorted(named["named"]) == sorted(ids)
    summary = client.get("/api/lineups?view=summary").json()
    (team,) = summary["teams"]
    assert team["team"] == "Koski Team" and len(team["games"]) == 2
    assert [g["player_count"] for g in summary["lineups"]] == [6, 6] and "thumbnails" not in str(summary)
    assert client.get("/api/faces?named=true").json()["identities"][0]["person"]["name"] == "Matt"
    assert client.get("/api/faces?q=koski").json()["total"] == 1
    names = client.get("/api/faces/names").json()
    assert names == {sources[1]["id"]: ["Koski Team", "Matt"]}
    in_source = client.get(f"/api/faces/sources/{sources[1]['id']}").json()
    assert in_source["automatic"] is None
    in_source = in_source["games"]
    assert len(in_source) == 2 and in_source[0]["people"][0]["link"]["name"] == "Matt"
    one = client.get(f"/api/lineups/{first['id']}").json()
    assert sum(p["link"] is not None for p in one["lineup"]["players"]) == 1
    # Stale or unrelated players cannot be named through a face.
    stranger = next(i["id"] for i in two_game[1:])
    response = client.put(
        f"/api/faces/players/{face['id']}/name",
        json={"player_ids": [stranger], "person_id": named["person_id"]},
    )
    assert response.status_code == 409
    # "Not this person" removes one game from Matt's face; undo restores it.
    matt = client.get(f"/api/faces/players/{face['id']}").json()
    assert matt["person"]["name"] == "Matt"
    other = two_game[1]
    other_detail = client.get(f"/api/faces/players/{other['id']}").json()
    pid = other_detail["appearances"][0]["id"]
    separation = client.post(
        f"/api/faces/players/{pid}/separate", json={"from_player_id": other_detail["appearances"][1]["id"]}
    ).json()
    assert client.get(f"/api/faces/players/{pid}").json()["games"] == 1
    assert client.post(f"/api/faces/separations/{separation['id']}/delete", json={}).status_code == 200
    assert client.get(f"/api/faces/players/{pid}").json()["games"] == 2


@pytest.mark.usefixtures("fake_lineup")
def test_deleting_a_game_removes_its_separations(db, sources):
    from hogline.db import LineupSeparation
    from hogline.domain.identities import Separate, separate
    from hogline.domain.lineups import Review, delete_lineup

    _, first = make_lineup(db, sources)
    _, second = make_lineup(db, sources, start_seconds=400, end_seconds=520)
    separate(db, first["players"][0]["id"], Separate(from_player_id=second["players"][0]["id"]))
    delete_lineup(db, first["id"], Review())
    with db.snapshot_session() as session:
        assert not list(session.scalars(select(LineupSeparation)))


def test_lineup_requests_carry_thumbnail_policy(db, sources, monkeypatch):
    monkeypatch.setattr(lineups, "lineup_models", lambda root, key=None: ("fake", "fake-face-v1"))
    request = lineups.LineupRequest(
        source_id=sources[1]["id"], start_seconds=0, end_seconds=600, max_thumbnails=64
    )
    batch = lineups.create_lineup_batch(db, request)
    assert batch["units"][0]["request"]["thumbnails"] == {"policy": "diverse-clear-faces-v1", "max": 64}


@pytest.mark.usefixtures("media")
def test_finished_recording_analysis_queues_its_faces(db, sources):
    from hogline.orchestration.recording import RecordingAnalysisRequest, start_recording_analysis
    from tests.test_recording_analysis import drain, small_source

    source_id = small_source(db, sources, 7200)
    start_recording_analysis(db, source_id, RecordingAnalysisRequest(detail_windows=2))
    drain(db)
    with db.snapshot_session() as session:
        row = session.get(AutoLineup, source_id)
        assert row.state == "waiting" and row.document["reason"] == "recording_analysis"
    # Without installed face models nothing starts, and the status says why.
    auto_lineups.advance_auto_lineups(db, force=True)
    status = auto_lineups.status(db)
    assert status["counts"] == {"waiting": 1} and "face-models --install" in status["blocked"]


@pytest.mark.usefixtures("fake_lineup")
def test_lineups_grouped_before_portraits_choose_from_stored_faces(db, sources):
    from sqlalchemy.orm.attributes import flag_modified

    from hogline.adapters.sources import digest
    from hogline.domain.identities import face_index
    from hogline.review.app import create_app

    _, lineup = make_lineup(db, sources)
    with db.snapshot_session(write=True) as session:
        row = session.get(GameLineup, lineup["id"])
        document = {
            **row.document,
            "players": [{k: v for k, v in p.items() if k != "portrait"} for p in row.document["players"]],
        }
        row.document, row.sha256 = document, digest(document)
        flag_modified(row, "document")
    with db.snapshot_session() as session:
        players = face_index(session, db.settings.data_dir)["players"]
    chosen = {p["portrait"]["chosen"] for p in players.values() if p["vector"] is not None}
    assert chosen == {"clear_face_of_stored"}
    player = next(p for p in players.values() if p["vector"] is not None)
    client = TestClient(create_app(db.settings))
    image = client.get(f"/api/lineups/{lineup['id']}/players/{player['id']}/portrait.jpg")
    assert image.status_code == 200 and image.headers["content-type"] == "image/jpeg"
