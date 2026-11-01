"""Download ended broadcasts before analysis when remote seeking is unavailable."""

import fcntl
import hashlib
import json
import math
import subprocess
from pathlib import Path

from hogline.db import Source, now
from hogline.domain.catalog import source_or_error


def has_verified_download(source):
    saved = source.get("verified_download") or {}
    duration = source.get("duration_seconds") or 0
    return bool(
        saved.get("sha256")
        and saved.get("path")
        and Path(saved["path"]).is_file()
        and abs(saved.get("duration_seconds", 0) - duration) <= 5
    )


def validate_download(path, expected_duration):
    from hogline.media.frames import capture

    if not expected_duration or not math.isfinite(expected_duration):
        raise ValueError("A known recording duration is required")
    probe = json.loads(
        subprocess.run(
            ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        ).stdout
    )
    video = next((s for s in probe["streams"] if s["codec_type"] == "video"), None)
    duration = float(probe["format"]["duration"])
    start = float((video or {}).get("start_time", probe["format"].get("start_time", 0)))
    if not video or abs(duration - expected_duration) > 5 or abs(start) > 1:
        raise ValueError("Downloaded recording does not cover the expected timeline from zero")
    samples = []
    for seconds in (0, duration / 2, max(0, duration - 2)):
        _, stamp = capture(str(path), seconds)
        actual = stamp["pts"] * stamp["time_base_num"] / stamp["time_base_den"]
        if abs(actual - seconds) > 1:
            raise ValueError("Downloaded recording has inconsistent seek timestamps")
        samples.append({"requested_seconds": seconds, "actual_seconds": actual})
    with path.open("rb") as handle:
        sha = hashlib.file_digest(handle, "sha256").hexdigest()
    return {
        "path": str(path.resolve()),
        "sha256": sha,
        "duration_seconds": duration,
        "expected_duration_seconds": expected_duration,
        "unavailable_tail_seconds": max(0, expected_duration - duration),
        "height": video["height"],
        "seek_checks": samples,
        "verified_at": now(),
    }


def prepare_download(db, source_id):
    # Serialize overlapping UI/CLI attempts without holding a database transaction.
    import uuid

    key = str(uuid.UUID(source_id))
    root = db.settings.data_dir / "post-live" / key
    root.mkdir(parents=True, exist_ok=True)
    with (root / "prepare.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _prepare_download(db, source_id)


def _prepare_download(db, source_id):
    """No database transaction is held while fetching/verifying media; resume interrupted downloads."""
    with db.snapshot_session() as session:
        source = dict(source_or_error(session, source_id))
    if has_verified_download(source):
        return source["verified_download"]
    if source.get("platform") != "youtube" or source.get("live_status") != "post_live":
        raise ValueError("Download fallback is only for ended YouTube streams")
    if not source.get("duration_seconds"):
        raise ValueError("Refresh recording metadata to obtain its duration first")
    root = db.settings.data_dir / "post-live" / source_id
    root.mkdir(parents=True, exist_ok=True)
    # Keep video only, at most 720p/4 GiB. Missing fragments must fail, never silently skip.
    subprocess.run(
        [
            "yt-dlp",
            "--ignore-config",
            "--no-playlist",
            "--abort-on-unavailable-fragments",
            "--retries",
            "3",
            "--fragment-retries",
            "3",
            "--socket-timeout",
            "20",
            "--concurrent-fragments",
            "4",
            "--max-filesize",
            "4G",
            "--no-progress",
            "-f",
            "bestvideo[height<=720]",
            "-o",
            str(root / "recording.%(ext)s"),
            source["url"],
        ],
        check=True,
        timeout=7200,
        capture_output=True,
    )
    paths = [p for p in root.glob("recording.*") if p.suffix in (".mp4", ".webm", ".mkv")]
    if len(paths) != 1:
        raise ValueError("Download did not produce one complete video")
    saved = validate_download(paths[0], source["duration_seconds"])
    return attach_download(db, source_id, source["metadata_sha256"], saved)


def attach_download(db, source_id, metadata_sha256, saved):
    with db.snapshot_session(write=True) as session:
        row = session.get(Source, source_id)
        if row is None or row.document["metadata_sha256"] != metadata_sha256:
            raise ValueError("Recording metadata changed; verify the download again")
        row.document = {
            **row.document,
            "verified_download": saved,
            "metadata_refresh_needed": False,
            "youtube_duration_seconds": row.document.get(
                "youtube_duration_seconds", row.document.get("duration_seconds")
            ),
            "duration_seconds": min(saved["duration_seconds"], row.document["duration_seconds"]),
        }
    return saved
