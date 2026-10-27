import copy
import json

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from hogline.adapters.sources import import_youtube, video_link
from hogline.analytics.exports import csv_export, snapshot
from hogline.db import Evidence, GameRow, MetadataSnapshot, Revision, Source
from hogline.domain.catalog import Conflict, catalog, save_game
from hogline.domain.models import End, Game, GameEdit, Result, ScanRequest, Span


def make_game(sources, **kwargs):
    return Game(
        sheet=5, spans=[{"source_id": sources[0]["id"], "start_seconds": 100, "end_seconds": 1000}], **kwargs
    )


def edit(db, game, revision=0, reason="Test review"):
    return save_game(
        db, GameEdit(expected_revision=revision, author="Fixture reviewer", reason=reason, game=game)
    )


def add_evidence(db, source_id, seconds=500):
    with db.session() as session:
        e = Evidence(
            source_id=source_id,
            evidence_key=f"fixture:{source_id}:{seconds}",
            document={
                "source_seconds": seconds,
                "pts": int(seconds * 90000),
                "time_base_num": 1,
                "time_base_den": 90000,
                "youtube_url": f"https://www.youtube.com/watch?v=AAAAAAAAAAA&t={seconds}s",
            },
        )
        session.add(e)
        session.flush()
        return e.id


def test_metadata_only_idempotent_distinct_dates_no_tokens(db, sources):
    first = sources[0]
    import_youtube(
        db,
        "https://youtu.be/AAAAAAAAAAA",
        {
            "id": "AAAAAAAAAAA",
            "title": "SHEET 5 | 01-16-26",
            "duration": 36000,
            "upload_date": "20260117",
            "formats": [{"format_id": "1", "height": 1080}],
        },
    )
    with db.session() as session:
        assert session.scalar(select(func.count()).select_from(Source)) == 2
        assert session.scalar(select(func.count()).select_from(MetadataSnapshot)) == 2
        text = json.dumps([r.document for r in session.scalars(select(MetadataSnapshot))])
        assert "secret.invalid" not in text and "must-not-be-saved" not in text
    assert first["title_date"] == "2026-01-16"
    assert first["upload_date"] == "20260117"
    assert first["recording_clock_date"] is None
    assert video_link(first, 500.99).endswith("&t=500s")


def test_multiple_games_per_video_multiple_videos_per_game(db, sources):
    game = make_game(sources)
    game.spans.append(
        Span(source_id=sources[1]["id"], start_seconds=0, end_seconds=1200, relationship="continuation")
    )
    game = Game.model_validate(game.model_dump())
    edit(db, game)
    another = Game(
        sheet=5, spans=[{"source_id": sources[0]["id"], "start_seconds": 3600, "end_seconds": 7200}]
    )
    edit(db, another)
    data = catalog(db)
    assert len(data["games"]) == 2
    assert len(next(g for g in data["games"] if g["id"] == game.id)["spans"]) == 2


def test_corrections_keep_ids_history_and_reject_stale_edits(db, sources):
    game = make_game(sources)
    first = edit(db, game)
    game.label = "Reviewer corrected the boundary"
    game.spans[0].start_seconds = 200
    second = edit(db, game, first["revision"])
    assert second["revision"] == 2 and second["id"] == first["id"]
    with pytest.raises(Conflict):
        edit(db, game, 1)
    with db.session() as session:
        rows = list(session.scalars(select(Revision).order_by(Revision.number)))
        assert [r.number for r in rows] == [1, 2]
        assert rows[0].document["spans"][0]["start_seconds"] == 100


def test_null_zero_and_score_evidence_contract():
    unknown = End(observed_index=1)
    assert unknown.red_points is None
    with pytest.raises(ValidationError):
        End(observed_index=1, red_points=0, yellow_points=0)
    with pytest.raises(ValidationError):
        End(observed_index=1, state="played", red_points=2, yellow_points=1, evidence_ids=["e"])
    with pytest.raises(ValidationError):
        End(observed_index=1, state="blank", red_points=0, yellow_points=0)
    assert (
        End(observed_index=1, state="blank", red_points=0, yellow_points=0, evidence_ids=["e"]).red_points
        == 0
    )


def test_evidence_must_belong_to_linked_span(db, sources):
    wrong = add_evidence(db, sources[1]["id"])
    game = make_game(
        sources,
        ends=[
            End(
                observed_index=1,
                state="played",
                red_points=2,
                yellow_points=0,
                acceptance="human_confirmed",
                evidence_ids=[wrong],
            )
        ],
    )
    with pytest.raises(ValueError, match="inside a linked"):
        edit(db, game)
    right = add_evidence(db, sources[0]["id"])
    game.ends[0].evidence_ids = [right]
    edit(db, game)


def test_partial_end_totals_do_not_invent_a_final_result(db, sources):
    evidence = add_evidence(db, sources[0]["id"])
    game = make_game(
        sources,
        coverage="partial",
        acceptance="human_confirmed",
        ends=[
            End(
                observed_index=1,
                official_number=5,
                state="played",
                red_points=2,
                yellow_points=0,
                acceptance="human_confirmed",
                evidence_ids=[evidence],
            )
        ],
    )
    edit(db, game)
    release = snapshot(db)
    assert release["coverage"]["confirmed_results"] == 0
    assert release["games"][0]["result"]["red_points"] is None
    assert "t=500s" in csv_export(release)


def test_release_is_immutable_and_excludes_unreviewed_scores(db, sources):
    evidence = add_evidence(db, sources[0]["id"])
    game = make_game(
        sources,
        acceptance="human_confirmed",
        ends=[
            End(
                observed_index=1,
                state="played",
                red_points=1,
                yellow_points=0,
                acceptance="proposed",
                evidence_ids=[evidence],
            )
        ],
    )
    edit(db, game)
    release = snapshot(db)
    frozen = copy.deepcopy(release)
    game.label = "A later correction"
    edit(db, game, 1)
    assert release == frozen
    assert release["games"][0]["label"] != game.label
    assert release["games"][0]["ends"] == []
    assert snapshot(db, "exploratory")["games"][0]["ends"][0]["red_points"] == 1


def test_final_result_needs_separate_completion_and_matching_outcome(sources):
    with pytest.raises(ValidationError):
        Result(outcome="red_win", red_points=2, yellow_points=4, evidence_ids=["e"])
    with pytest.raises(ValidationError):
        make_game(
            sources, result=Result(outcome="red_win", red_points=4, yellow_points=2, evidence_ids=["e"])
        )


def test_bounds_are_checked_before_work_is_enqueued():
    with pytest.raises(ValidationError):
        ScanRequest(source_id="s", end_seconds=36000)
    with pytest.raises(ValidationError):
        ScanRequest(source_id="s", end_seconds=1200, interval_seconds=1, max_frames=120)
    with pytest.raises(ValidationError):
        ScanRequest(source_id="s", end_seconds=float("inf"))


def test_source_duration_and_sheet_are_validated(db, sources):
    game = make_game(sources)
    game.spans[0].end_seconds = 40000
    with pytest.raises(ValueError, match="past the recording"):
        edit(db, game)
    game.spans[0].end_seconds = 2000
    game.sheet = 4
    with pytest.raises(ValueError, match="different sheet"):
        edit(db, game)
    with db.session() as session:
        assert session.scalar(select(func.count()).select_from(GameRow)) == 0
