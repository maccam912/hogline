import shutil
import subprocess

import pytest

from hogline.adapters.sources import import_youtube
from hogline.config import Settings
from hogline.db import Database


@pytest.fixture
def db(tmp_path):
    return Database(Settings.load(tmp_path))


@pytest.fixture
def sources(db):
    return [
        import_youtube(
            db,
            f"https://www.youtube.com/watch?v={vid}",
            {
                "id": vid,
                "title": "SHEET 5 | 01-16-26",
                "duration": duration,
                "upload_date": "20260117",
                "formats": [{"format_id": "1", "url": "https://secret.invalid/signed-token", "height": 1080}],
                "http_headers": {"Cookie": "must-not-be-saved"},
            },
        )
        for vid, duration in [("AAAAAAAAAAA", 36000), ("BBBBBBBBBBB", 7200)]
    ]


@pytest.fixture
def video(tmp_path):
    if not shutil.which("ffmpeg"):
        pytest.skip("FFmpeg integration requires ffmpeg on PATH")
    path = tmp_path / "synthetic.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=160x90:rate=10",
            "-t",
            "3",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        check=True,
    )
    return path
