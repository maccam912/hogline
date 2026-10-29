"""Disposable stream-copy sections for repeated local seeks, retaining source PTS."""

import json
import subprocess
import tempfile
import time
from fractions import Fraction
from pathlib import Path

from hogline.media.errors import remote_options


def probe_stream(uri):
    command = ["ffprobe", "-v", "error"]
    command += remote_options(uri)
    command += [
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=time_base,start_pts,bit_rate:format=bit_rate",
        "-of",
        "json",
        uri,
    ]
    result = subprocess.run(command, capture_output=True, check=True, timeout=45)
    metadata = json.loads(result.stdout)
    stream = metadata["streams"][0]
    bitrate = int(stream.get("bit_rate") or metadata.get("format", {}).get("bit_rate") or 0)
    return Fraction(stream["time_base"]), int(stream.get("start_pts", 0)), bitrate


class VideoChunk:
    def __init__(self, root, max_mb):
        self.root, self.max_mb = root, max_mb
        self.directory = None
        self.path = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        if self.directory:
            self.directory.cleanup()

    def prepare(self, uri, start, end, tick, *, seek_seconds=None):
        if not self.max_mb or not uri.startswith("https://"):
            return
        self.root.mkdir(parents=True, exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(prefix="video-", dir=self.root)
        path = Path(self.directory.name) / "section.nut"
        try:
            self.source_tb, self.source_pts, bitrate = probe_stream(uri)
            tick()
            # Input-side duration is required with copyts and nonzero source origins.
            command = [
                "ffmpeg",
                "-hide_banner",
                "-nostdin",
                "-loglevel",
                "error",
                *remote_options(uri),
                "-copyts",
                "-ss",
                str(start),
                "-t",
                str(end - start),
                "-i",
                uri,
                "-map",
                "0:v:0",
                "-an",
                "-c:v",
                "copy",
                "-copytb",
                "1",
                "-avoid_negative_ts",
                "disabled",
                "-fs",
                str(self.max_mb * 1024**2),
                "-y",
                str(path),
            ]
            process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            started = time.monotonic()
            try:
                while process.poll() is None:
                    tick()
                    elapsed = time.monotonic() - started
                    deadline = min(120, seek_seconds) if seek_seconds is not None else 120
                    if elapsed > deadline:
                        raise ValueError("Temporary video acquisition timed out")
                    if seek_seconds is not None and bitrate > 0 and elapsed > 3:
                        downloaded = path.stat().st_size if path.exists() else 0
                        projected = (end - start) * bitrate / 8 * elapsed / max(1, downloaded)
                        if projected > seek_seconds:
                            raise ValueError("Remote frame seeks are faster than downloading this section")
                    time.sleep(0.1)
                if process.returncode or path.stat().st_size >= self.max_mb * 1024**2:
                    raise ValueError("Temporary video section exceeded its allowance or was unavailable")
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)
            chunk_tb, chunk_pts, _ = probe_stream(str(path))
            self.origin = chunk_tb * chunk_pts
            self.path = path
        except (OSError, ValueError, KeyError, IndexError, subprocess.SubprocessError):
            # Preserve the existing per-frame acquisition path when remuxing is unavailable.
            self.directory.cleanup()
            self.directory = None
            tick()  # A cancelled/over-budget job must never fall back to more acquisition.

    def capture(self, decoder, uri, seconds):
        if self.path is None:
            return decoder(uri, seconds)
        seek = float(Fraction(str(seconds)) + self.source_tb * self.source_pts - self.origin)
        try:
            image, timing = decoder(str(self.path), seek)
        except ValueError:
            # An incomplete temporary section must not break a frame that the
            # original stream can still supply. That stream retains its own PTS checks.
            return decoder(uri, seconds)
        pts = Fraction(timing["pts"] * timing["time_base_num"], timing["time_base_den"]) / self.source_tb
        if pts.denominator != 1:
            raise ValueError("Temporary remux cannot reproduce an original source presentation timestamp")
        return image, {
            "pts": int(pts),
            "time_base_num": self.source_tb.numerator,
            "time_base_den": self.source_tb.denominator,
        }
