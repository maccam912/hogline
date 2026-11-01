import json
import subprocess

import pytest

from hogline.adapters.sources import import_youtube
from hogline.media import post_live
from hogline.media.frames import resolve_media
from hogline.orchestration.recording import RecordingAnalysisRequest, start_recording_analysis


def test_verified_download_is_used_for_analysis(db, video):
    source = import_youtube(
        db,
        "https://www.youtube.com/watch?v=AAAAAAAAAAA",
        {
            "id": "AAAAAAAAAAA",
            "title": "Ended game",
            "duration": 3,
            "live_status": "post_live",
        },
    )
    saved = post_live.validate_download(video, 3)
    post_live.attach_download(db, source["id"], source["metadata_sha256"], saved)
    from hogline.db import Source

    with db.snapshot_session() as session:
        current = session.get(Source, source["id"]).document
    assert post_live.has_verified_download(current)
    assert resolve_media(current)["uri"] == str(video.resolve())
    assert start_recording_analysis(db, source["id"], RecordingAnalysisRequest())["state"] == "active"
    video.write_bytes(b"changed")
    with pytest.raises(ValueError, match="changed"):
        resolve_media(current)


def test_short_or_offset_download_is_rejected(video, monkeypatch):
    with pytest.raises(ValueError, match="expected timeline"):
        post_live.validate_download(video, 100)
    real_run = subprocess.run

    def run(cmd, **kwargs):
        result = real_run(cmd, **kwargs)
        if cmd[0] == "ffprobe":
            data = json.loads(result.stdout)
            data["streams"][0]["start_time"] = "3600"
            result.stdout = json.dumps(data)
        return result

    monkeypatch.setattr(post_live.subprocess, "run", run)
    with pytest.raises(ValueError, match="expected timeline"):
        post_live.validate_download(video, 3)


def test_missing_download_does_not_bypass_gate(tmp_path):
    assert not post_live.has_verified_download(
        {
            "duration_seconds": 3,
            "verified_download": {
                "path": str(tmp_path / "missing.mp4"),
                "sha256": "abc",
                "duration_seconds": 3,
            },
        }
    )
