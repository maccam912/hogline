from types import SimpleNamespace

import pytest
from sqlalchemy import select

from hogline.config import pretrained_model_path
from hogline.db import GameRow, Source
from hogline.orchestration import recording_followups as followups


def test_pretrained_models_survive_data_reset(tmp_path, monkeypatch):
    monkeypatch.delenv("HOGLINE_MODELS_DIR", raising=False)
    data = tmp_path / "data"
    path = pretrained_model_path(data, "face/model.onnx")
    assert path == tmp_path / "hogline-assets/models/face/model.onnx"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"pinned model")
    assert pretrained_model_path(data, "face/model.onnx").read_bytes() == b"pinned model"


def test_followups_are_idempotent_and_do_not_confirm_teams(db, sources, monkeypatch):
    sid = sources[0]["id"]
    with db.snapshot_session(write=True) as session:
        row = session.get(Source, sid)
        row.document = {
            **row.document,
            "duration_seconds": 2000,
            "title_teams": ["Koski, Lindsey", "Dobbs, Jason"],
        }
    calls = {}

    def start(db, game_id, request, *, automatic=False):
        assert automatic
        calls[(game_id, request.start_seconds)] = request.end_seconds

    monkeypatch.setattr(
        followups, "load_vision_config", lambda: SimpleNamespace(enabled=True, api_key="fake")
    )
    monkeypatch.setattr(followups, "start_campaign", start)
    monkeypatch.setattr(followups, "preview_batch", lambda *args: {"plan": {"units": []}})
    first = followups.advance_source(db, sid)
    assert followups.advance_source(db, sid) == first
    assert len(calls) == 2
    with db.snapshot_session() as session:
        games = list(session.scalars(select(GameRow)))
        assert len(games) == 1
        game = games[0].document
        assert game["acceptance"] == "proposed"
        assert game["completion"] == "unknown"
        assert game["red_team"] == "Red · unidentified"


def test_disabled_vision_does_not_create_or_queue_work(db, sources, monkeypatch):
    monkeypatch.setattr(followups, "load_vision_config", lambda: SimpleNamespace(enabled=False, api_key=""))
    with pytest.raises(ValueError, match="Enable vision"):
        followups.advance_source(db, sources[0]["id"])
    with db.snapshot_session() as session:
        assert not list(session.scalars(select(GameRow)))


def test_disabled_vision_still_schedules_local_motion(db, sources, monkeypatch):
    from hogline.orchestration.skill_motion_batches import MotionBatchRequest

    sid = sources[0]["id"]
    with db.snapshot_session(write=True) as session:
        session.add(
            GameRow(
                id="game", document={"spans": [{"source_id": sid, "start_seconds": 0, "end_seconds": 100}]}
            )
        )
    monkeypatch.setattr(followups, "load_vision_config", lambda: SimpleNamespace(enabled=False, api_key=""))
    monkeypatch.setattr(followups, "start_campaign", lambda *a, **k: pytest.fail("Vision was disabled"))
    monkeypatch.setattr(
        followups, "preview_batch", lambda *a: {"plan": {"units": [1]}, "plan_sha256": "a" * 64}
    )
    started = []
    monkeypatch.setattr(followups, "start_batch", lambda *a: started.append(a[2]))
    assert followups.advance_source(db, sid) == ["game"]
    assert started[0].request == MotionBatchRequest(max_candidates=4)


@pytest.mark.parametrize("live_status", ["is_live", "is_upcoming", "post_live"])
def test_unfinished_recording_cannot_start_skill_work(db, sources, live_status):
    sid = sources[0]["id"]
    with db.snapshot_session(write=True) as session:
        source = session.get(Source, sid)
        source.document = {**source.document, "live_status": live_status}
    with pytest.raises(ValueError, match="complete, seekable"):
        followups.advance_source(db, sid)


def test_recording_status_reports_missing_models_instead_of_empty_completion(db, sources, monkeypatch):
    from hogline.db import AutoLineup
    from hogline.review import recording_status

    monkeypatch.setattr(recording_status, "models_problem", lambda root: "Face model missing")
    with db.snapshot_session(write=True) as session:
        session.add(AutoLineup(source_id=sources[0]["id"], state="waiting", document={}))
    result = recording_status.recording_status(db, sources[0]["id"])
    assert result["faces"]["state"] == "waiting"
    assert result["faces"]["blocker"] == "Face model missing"
    assert result["scores"]["state"] == "not_started"
    assert result["skills"] == []
