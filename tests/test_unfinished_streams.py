"""Recordings added while live: clear refusals instead of empty decoding windows."""

import subprocess

import pytest

from hogline.adapters.sources import import_youtube
from hogline.db import AutoLineup, Source
from hogline.media import frames
from hogline.media.errors import TransientMediaError
from hogline.orchestration import auto_lineups, lineups


def live_source(db, live_status="post_live", duration=None):
    return import_youtube(
        db,
        "https://www.youtube.com/watch?v=LIVELIVELIV",
        {
            "id": "LIVELIVELIV",
            "title": "Oct 30 — Friday 6:15 - Mixed League — Koski vs Dobbs (Sheet 4)",
            "duration": duration,
            "live_status": live_status,
            "upload_date": "20261030",
        },
    )


def test_live_metadata_is_kept_and_marked_for_refresh(db):
    source = live_source(db)
    assert source["live_status"] == "post_live" and source["metadata_refresh_needed"] is True
    finished = import_youtube(
        db,
        "https://www.youtube.com/watch?v=LIVELIVELIV",
        {"id": "LIVELIVELIV", "title": "Koski vs Dobbs", "duration": 7479, "live_status": "was_live"},
    )
    assert finished["duration_seconds"] == 7479 and finished["metadata_refresh_needed"] is False


def test_post_live_media_fails_with_a_clear_reason(monkeypatch):
    def run(cmd, **_):
        assert "%(live_status)s" in cmd[cmd.index("--print") + 1]
        return subprocess.CompletedProcess(cmd, 0, "299\thttps://example.invalid/v\tpost_live\n", "")

    monkeypatch.setattr(frames.subprocess, "run", run)
    with pytest.raises(TransientMediaError, match="still processing this ended live stream"):
        frames.resolve_media({"platform": "youtube", "url": "https://www.youtube.com/watch?v=x"})
    monkeypatch.setattr(
        frames.subprocess,
        "run",
        lambda cmd, **_: subprocess.CompletedProcess(cmd, 0, "18\thttps://example.invalid/v\twas_live\n", ""),
    )
    assert frames.resolve_media({"platform": "youtube", "url": "u"})["format_id"] == "18"


def test_lineups_and_analysis_refuse_unfinished_streams(db, monkeypatch):
    from hogline.orchestration.recording import RecordingAnalysisRequest, start_recording_analysis

    source = live_source(db, duration=None)
    monkeypatch.setattr(lineups, "lineup_models", lambda root, key=None: ("fake", "fake-face-v1"))
    request = lineups.LineupRequest(source_id=source["id"], start_seconds=0, end_seconds=90)
    with pytest.raises(ValueError, match="YouTube is still processing it"):
        lineups.create_lineup_batch(db, request)
    with pytest.raises(ValueError, match="Complete the recording metadata"):
        start_recording_analysis(db, source["id"], RecordingAnalysisRequest())
    processing = live_source(db, duration=7479)
    with pytest.raises(ValueError, match="has not finished this live stream"):
        start_recording_analysis(db, processing["id"], RecordingAnalysisRequest())
    # Queued for faces, it needs attention (not "too short") and asks for fresh metadata.
    monkeypatch.setattr(auto_lineups, "lineup_models", lambda root, key=None: ("fake", "fake-face-v1"))
    with db.snapshot_session(write=True) as session:
        session.get(Source, source["id"]).document = {
            **session.get(Source, source["id"]).document,
            "metadata_refresh_needed": False,
        }
    auto_lineups.control(db, source["id"], "queue")
    auto_lineups.advance_auto_lineups(db, force=True)
    with db.snapshot_session() as session:
        row = session.get(AutoLineup, source["id"])
        assert row.state == "attention" and "YouTube is still processing" in row.document["problem"]
        assert session.get(Source, source["id"]).document["metadata_refresh_needed"] is True


def test_a_finished_analysis_requeues_unfinished_or_auto_skipped_recordings(db):
    source = live_source(db)
    with db.snapshot_session(write=True) as session:
        row = auto_lineups.request_in_session(session, source["id"], "operator")
        auto_lineups._set(row, "attention", "unfinished_recording", problem="unfinished")
    with db.snapshot_session(write=True) as session:
        assert auto_lineups.request_in_session(session, source["id"], "recording_analysis").state == "waiting"
    auto_lineups.control(db, source["id"], "skip")
    with db.snapshot_session(write=True) as session:
        assert auto_lineups.request_in_session(session, source["id"], "recording_analysis").state == "skipped"
