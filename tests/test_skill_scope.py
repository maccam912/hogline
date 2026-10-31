import copy

import pytest

from hogline.db import GameRow
from hogline.domain.catalog import Conflict, save_game
from hogline.domain.models import Game, GameEdit
from hogline.domain.skill_scope import (
    CoverageExtension,
    compatible_game_scope,
    extend_skill_coverage,
    widening_only,
)
from hogline.domain.skills import Shot, SkillDocument, SkillEdit, read_skills, save_skills


def make_game(db, sources):
    game = Game(
        id="game",
        sheet=5,
        coverage="partial",
        spans=[
            {
                "source_id": sources[0]["id"],
                "start_seconds": 10,
                "end_seconds": 100,
            }
        ],
    )
    save_game(db, GameEdit(expected_revision=0, author="test", reason="Partial review", game=game))
    return game


def extension(revision=1, start=0, end=200):
    return CoverageExtension(
        expected_game_revision=revision, span_index=0, start_seconds=start, end_seconds=end
    )


def test_monotonic_coverage_extension_keeps_review_evidence_and_history(db, sources):
    game = make_game(db, sources)
    save_skills(
        db,
        "game",
        SkillEdit(
            expected_revision=0,
            document=SkillDocument(
                shots=[
                    Shot(id="shot", source_id=sources[0]["id"], seconds=30, note="Existing review"),
                ]
            ),
        ),
    )
    result = extend_skill_coverage(db, "game", extension())
    assert result["game_revision"] == 2 and result["spans"][0]["end_seconds"] == 200
    assert extend_skill_coverage(db, "game", extension(2))["game_revision"] == 2
    extend_skill_coverage(db, "game", extension(2, end=300))
    with db.snapshot_session() as session:
        row = session.get(GameRow, "game")
        assert compatible_game_scope(session, row, 1)
        current = read_skills(session, "game")
        assert not current["stale"] and current["revision"] == 1
        assert current["document"]["shots"][0]["note"] == "Existing review"
        assert row.document["acceptance"] == game.acceptance
        assert row.document["ends"] == [] and row.document["coverage"] == "partial"


def test_shrinks_semantic_edits_missing_history_and_changed_source_are_not_compatible(db, sources):
    original = make_game(db, sources)
    for change in ({"label": "Different interpretation"}, {"red_team": "Other team"}, {"ends": []}):
        altered = copy.deepcopy(original.model_dump())
        altered.update(change)
        if change != {"ends": []}:
            assert not widening_only(original.model_dump(), altered)
    extend_skill_coverage(db, "game", extension())
    with db.snapshot_session() as session:
        current = Game.model_validate(session.get(GameRow, "game").document)
    current.spans[0].start_seconds = 20
    save_game(db, GameEdit(expected_revision=2, author="test", reason="Shrink boundary", game=current))
    current.spans[0].start_seconds = 0
    save_game(db, GameEdit(expected_revision=3, author="test", reason="Restore extent", game=current))
    with db.snapshot_session() as session:
        assert not compatible_game_scope(session, session.get(GameRow, "game"), 1)
    with db.snapshot_session(write=True) as session:
        row = session.get(GameRow, "game")
        row.document = {**row.document, "label": "Unrecorded change"}
    with db.snapshot_session() as session:
        assert not compatible_game_scope(session, session.get(GameRow, "game"), 4)
    alternate = original.model_dump()
    alternate["spans"][0]["source_id"] = sources[1]["id"]
    assert not widening_only(original.model_dump(), alternate)


def test_coverage_route_rejects_conflicts_shrinks_recording_overruns_and_accepted_games(db, sources):
    from fastapi.testclient import TestClient

    from hogline.review.app import create_app

    make_game(db, sources)
    client = TestClient(create_app(db.settings))
    route = "/api/games/game/skills/coverage"
    assert client.post(route, json=extension().model_dump()).status_code == 200
    assert client.post(route, json=extension().model_dump()).status_code == 409
    assert client.post(route, json=extension(2, start=20).model_dump()).status_code == 422
    assert client.post(route, json=extension(2, end=40000).model_dump()).status_code == 422
    with db.snapshot_session() as session:
        game = Game.model_validate(session.get(GameRow, "game").document)
    game.acceptance = "human_confirmed"
    save_game(db, GameEdit(expected_revision=2, author="test", reason="Accept boundaries", game=game))
    assert client.post(route, json=extension(3, end=300).model_dump()).status_code == 422
    with pytest.raises(Conflict):
        extend_skill_coverage(db, "game", extension())
