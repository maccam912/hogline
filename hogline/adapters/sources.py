import hashlib
import json
import re
import subprocess
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from sqlalchemy import select

from hogline.db import Database, MetadataSnapshot, Source, now


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def run_json(args, timeout=60):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=True)
    except FileNotFoundError as exc:
        raise ValueError(f"Required executable missing: {args[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ValueError(f"{args[0]} timed out; retry this bounded request") from exc
    except subprocess.CalledProcessError as exc:
        # Avoid exposing signed media URLs or local credential details in logs/API errors.
        raise ValueError(f"{args[0]} failed (exit {exc.returncode}); source may be unavailable") from exc
    return json.loads(result.stdout)


def youtube_id(url):
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host == "youtu.be":
        value = parsed.path.strip("/")
    elif host in ("youtube.com", "www.youtube.com", "m.youtube.com"):
        value = parse_qs(parsed.query).get("v", [""])[0]
        if parsed.path.startswith(("/live/", "/embed/", "/shorts/")):
            value = parsed.path.split("/")[2]
    else:
        raise ValueError("Use a YouTube recording URL or the CLI local-file importer")
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", value):
        raise ValueError("Could not find a valid YouTube recording ID")
    return value


def video_link(source, seconds=0):
    if source.get("platform") == "youtube":
        return f"https://www.youtube.com/watch?v={source['platform_id']}&t={max(0, int(seconds))}s"
    return None


def title_hints(title):
    sheet = re.search(r"SHEET\s*(\d+)", title, re.I)
    date = re.search(r"\b(\d{2}-\d{2}-\d{2})\b", title)
    try:
        day = datetime.strptime(date[1], "%m-%d-%y").date().isoformat() if date else None
    except ValueError:
        day = None
    return {"sheet": int(sheet[1]) if sheet and 1 <= int(sheet[1]) <= 8 else None, "title_date": day}


def curated_metadata(raw):
    keys = (
        "id",
        "title",
        "description",
        "channel_id",
        "channel",
        "duration",
        "upload_date",
        "timestamp",
        "release_timestamp",
        "availability",
        "live_status",
        "width",
        "height",
        "fps",
    )
    result = {k: raw.get(k) for k in keys}
    result["formats"] = [
        {k: f.get(k) for k in ("format_id", "ext", "width", "height", "fps", "vcodec", "acodec", "filesize")}
        for f in raw.get("formats", [])
    ]
    # Do not persist temporary signed format URLs, cookies, or request headers.
    return result


def upsert_source(db: Database, key: str, metadata: dict, platform: str):
    fingerprint = digest(metadata)
    with db.session() as session:
        row = session.scalar(select(Source).where(Source.source_key == key))
        if row is None:
            row = Source(source_key=key, document={})
            session.add(row)
            session.flush()
        old = row.document
        title = metadata.get("title", "Untitled source")
        row.document = {
            **old,
            "id": row.id,
            "platform": platform,
            "platform_id": metadata.get("id"),
            "title": title,
            "duration_seconds": metadata.get("duration"),
            **title_hints(title),
            "upload_date": metadata.get("upload_date"),
            "recording_clock_date": None,
            "event_date": None,
            "timezone": "America/Chicago",
            "clock_status": "unresolved",
            "availability": metadata.get("availability") or "unknown",
            "metadata_sha256": fingerprint,
            "last_checked_at": now(),
        }
        if platform == "youtube":
            row.document = {**row.document, "url": video_link(row.document)}
        else:
            row.document = {
                **row.document,
                "local_path": metadata["local_path"],
                "media_sha256": metadata["sha256"],
                "probe": metadata["probe"],
            }
        existing = session.scalar(
            select(MetadataSnapshot).where(
                MetadataSnapshot.source_id == row.id, MetadataSnapshot.sha256 == fingerprint
            )
        )
        if existing is None:
            session.add(MetadataSnapshot(source_id=row.id, sha256=fingerprint, document=metadata))
        return row.document


def import_youtube(db: Database, url: str, metadata: dict | None = None):
    vid = youtube_id(url)
    if metadata is None:
        metadata = run_json(
            [
                "yt-dlp",
                "--ignore-config",
                "--skip-download",
                "--no-playlist",
                "--socket-timeout",
                "15",
                "--retries",
                "1",
                "--dump-single-json",
                f"https://www.youtube.com/watch?v={vid}",
            ]
        )
    if metadata.get("id") != vid:
        raise ValueError("Metadata ID does not match the requested recording")
    return upsert_source(db, f"youtube:{vid}", curated_metadata(metadata), "youtube")


def probe(path):
    raw = run_json(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(path),
        ]
    )
    if not raw.get("streams"):
        raise ValueError("No video stream found")
    stream = raw["streams"][0]
    num, den = map(int, stream["time_base"].split("/"))
    return {
        "width": stream["width"],
        "height": stream["height"],
        "time_base_num": num,
        "time_base_den": den,
        "start_pts": int(stream.get("start_pts", 0)),
        "duration": float(stream.get("duration", raw["format"].get("duration", 0))),
        "nominal_frame_rate": stream.get("avg_frame_rate"),
        "codec": stream.get("codec_name"),
    }


def import_local(db: Database, path: str, sheet: int):
    if not 1 <= sheet <= 8:
        raise ValueError("Sheet must be 1 through 8")
    file = Path(path).resolve(strict=True)
    if not file.is_file():
        raise ValueError("Expected a local media file")
    with file.open("rb") as handle:
        sha = hashlib.file_digest(handle, "sha256").hexdigest()
    info = probe(file)
    metadata = {
        "title": f"SHEET {sheet} | {file.name}",
        "id": sha,
        "sha256": sha,
        "local_path": str(file),
        "duration": info["duration"],
        "probe": info,
        "availability": "local",
    }
    return upsert_source(db, f"local:{sha}", metadata, "local")


def inventory(db: Database, url: str, limit: int = 12):
    if not 1 <= limit <= 50:
        raise ValueError("Inventory is bounded to 1–50 recordings per request")
    parsed = urlparse(url)
    if parsed.hostname not in ("www.youtube.com", "youtube.com"):
        raise ValueError("Inventory expects a YouTube channel or playlist URL")
    raw = run_json(
        [
            "yt-dlp",
            "--ignore-config",
            "--flat-playlist",
            "--skip-download",
            "--playlist-end",
            str(limit),
            "--socket-timeout",
            "15",
            "--retries",
            "1",
            "--dump-single-json",
            url,
        ],
        timeout=120,
    )
    return [
        import_youtube(db, f"https://www.youtube.com/watch?v={entry['id']}", entry)
        for entry in raw.get("entries", [])
        if entry and entry.get("id")
    ]
