import hashlib
import io
import os
import re
import subprocess
from pathlib import Path

import numpy as np
from PIL import Image

from hogline.adapters.sources import digest, video_link
from hogline.config import profile
from hogline.media.errors import TransientMediaError, remote_options

_shared_cache_usage = {}


def cache_size(root):
    return sum(
        p.stat().st_size
        for folder, pattern in (("evidence", "*.png"), ("dense", "*.parquet"))
        for p in (root / folder).rglob(pattern)
    )


def share_cache_usage(root, counter):
    key = str(root.resolve())
    if counter is None:
        _shared_cache_usage.pop(key, None)
    else:
        _shared_cache_usage[key] = counter


def resolve_media(source, max_height=1080):
    if source["platform"] == "local":
        path = Path(source["local_path"])
        if not path.is_file():
            raise ValueError("Registered original media is missing; restore it or reimport its new location")
        with path.open("rb") as handle:
            if hashlib.file_digest(handle, "sha256").hexdigest() != source["media_sha256"]:
                raise ValueError("Registered original media changed; reimport it as a new source")
        return {"uri": str(path), "format_id": "local-original", "sha256": source["media_sha256"]}
    try:
        result = subprocess.run(
            [
                "yt-dlp",
                "--ignore-config",
                "--no-playlist",
                "--socket-timeout",
                "15",
                "--retries",
                "1",
                "-f",
                f"best[height<={max_height}]/bestvideo[height<={max_height}]",
                "--print",
                "%(format_id)s\t%(url)s",
                source["url"],
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
    except FileNotFoundError as exc:
        raise ValueError("Install yt-dlp to access remote recordings") from exc
    except subprocess.SubprocessError as exc:
        raise TransientMediaError(
            "Cannot access the recording. Metadata and timestamp links remain available."
        ) from exc
    try:
        format_id, url = result.stdout.strip().splitlines()[0].split("\t", 1)
    except (IndexError, ValueError) as exc:
        raise ValueError("The media adapter returned no usable selected format") from exc
    if not url.startswith("https://"):
        raise ValueError("The media adapter did not return an HTTPS stream")
    return {"uri": url, "format_id": format_id}


def capture(media: str, seconds: float):
    command = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "info"]
    command += remote_options(media)
    command += [
        "-copyts",
        "-ss",
        str(seconds),
        "-i",
        media,
        "-map",
        "0:v:0",
        "-frames:v",
        "1",
        "-vf",
        "showinfo",
        "-an",
        "-fps_mode",
        "vfr",
        "-f",
        "image2pipe",
        "-vcodec",
        "png",
        "pipe:1",
    ]
    try:
        output = subprocess.run(command, capture_output=True, timeout=45, check=True)
    except FileNotFoundError as exc:
        raise ValueError("Install FFmpeg to decode video frames") from exc
    except subprocess.SubprocessError as exc:
        error = TransientMediaError if media.startswith("https://") else ValueError
        raise error(f"Frame at {seconds:g}s unavailable; no observation was inferred") from exc
    log = output.stderr.decode(errors="replace")
    timebase = re.search(r"config in time_base:\s*(\d+)/(\d+)", log)
    frame = re.search(r"n:\s*0\s+pts:\s*(-?\d+)\s+pts_time:([\d.e+-]+)", log)
    if not output.stdout and media.startswith("https://"):
        raise TransientMediaError(f"Frame at {seconds:g}s unavailable; no observation was inferred")
    if not output.stdout or not timebase or not frame:
        raise ValueError("Decoder returned no frame with an original presentation timestamp")
    return Image.open(io.BytesIO(output.stdout)).convert("RGB"), {
        "pts": int(frame[1]),
        "time_base_num": int(timebase[1]),
        "time_base_den": int(timebase[2]),
    }


class EvidenceLimitError(ValueError):
    def __init__(self, message, scope):
        super().__init__(message)
        self.scope = scope


class EvidenceBudget:
    def __init__(self, root, job_mb, cache_mb):
        self.job_limit, self.cache_limit = job_mb * 1024**2, cache_mb * 1024**2
        self.logical_bytes = 0
        self.shared_usage = _shared_cache_usage.get(str(root.resolve()))
        self.cache_bytes = self.shared_usage.value if self.shared_usage is not None else cache_size(root)
        self.reserve_growth = None

    def consume(self, size, additional_bytes=0):
        if self.shared_usage is not None:
            with self.shared_usage.get_lock():
                self.cache_bytes = self.shared_usage.value
                self._consume(size, additional_bytes)
                self.shared_usage.value += max(0, additional_bytes)
        else:
            self._consume(size, additional_bytes)

    def _consume(self, size, additional_bytes):
        if self.logical_bytes + size > self.job_limit:
            raise EvidenceLimitError(
                "Evidence budget reached. Shorten the scan or explicitly raise its budget.", "job"
            )
        if self.cache_bytes + additional_bytes > self.cache_limit:
            raise EvidenceLimitError(
                "Shared evidence cache is full. Increase HOGLINE_MAX_CACHE_MB or archive cached media.",
                "shared",
            )
        if additional_bytes > 0 and self.reserve_growth:
            self.reserve_growth(additional_bytes)
        self.logical_bytes += size
        self.cache_bytes += additional_bytes


def save_image(image, root, budget=None):
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    content = buffer.getvalue()
    sha = hashlib.sha256(content).hexdigest()
    relative = f"evidence/{sha[:2]}/{sha}.png"
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    valid = path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == sha
    if budget:
        budget.consume(
            len(content), 0 if valid else len(content) - (path.stat().st_size if path.exists() else 0)
        )
    if not valid:
        temp = path.with_suffix(f".{os.getpid()}.tmp")
        temp.write_bytes(content)
        os.replace(temp, path)
    return {
        "path": relative,
        "sha256": sha,
        "bytes": len(content),
        "width": image.width,
        "height": image.height,
    }


def source_seconds(timing, source):
    info = source.get("probe", {})
    return timing["pts"] * timing["time_base_num"] / timing["time_base_den"] - (
        info.get("start_pts", 0) * info.get("time_base_num", 1) / info.get("time_base_den", 1)
    )


def observe(
    image, timing, source, root, budget=None, layout=None, composite_asset=None, acquisition_tier="evidence"
):
    layout = layout or profile()
    seconds = source_seconds(timing, source)
    result = {
        "source_id": source["id"],
        "source_metadata_sha256": source["metadata_sha256"],
        **timing,
        "source_seconds": seconds,
        "youtube_url": video_link(source, seconds),
        "view": "composite",
        "clock_status": "unresolved",
        "layout_profile": layout["id"],
        "profile_sha256": digest(layout),
        "crop_status": "unverified_seed_crops",
        "acquisition_tier": acquisition_tier,
        "measurement_quality": "coarse_scan_only"
        if acquisition_tier == "coarse"
        else "native_selected_encode",
        "image": composite_asset or save_image(image, root, budget),
        "crops": {},
    }
    vectors = {}
    for name, bounds in layout["views"].items():
        x1, y1, x2, y2 = bounds
        crop = image.crop(
            (
                round(x1 * image.width),
                round(y1 * image.height),
                round(x2 * image.width),
                round(y2 * image.height),
            )
        )
        result["crops"][name] = {**save_image(crop, root, budget), "normalized_bounds": bounds}
        vectors[name] = np.asarray(crop.convert("L").resize((96, 96)), dtype=np.float32)
    key = {
        "source": source["id"],
        "source_snapshot": source["metadata_sha256"],
        **timing,
        "image": result["image"]["sha256"],
        "profile_sha256": digest(layout),
    }
    if acquisition_tier == "coarse":
        key["acquisition_tier"] = "coarse-v1"
    result["evidence_key"] = digest(key)
    return result, vectors


def image_content(image):
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def read_image_bytes(root, asset):
    path = (root / asset["path"]).resolve()
    if not path.is_relative_to((root / "evidence").resolve()):
        raise ValueError("Evidence image path is outside the evidence archive")
    if "derived_from" in asset:
        if "derived_from" in asset["derived_from"]:
            raise ValueError("Nested evidence crops are not supported")
        parent = read_image(root, asset["derived_from"])
        bounds = asset["pixel_bounds"]
        if (
            len(bounds) != 4
            or not all(isinstance(v, int) for v in bounds)
            or not 0 <= bounds[0] < bounds[2] <= parent.width
            or not 0 <= bounds[1] < bounds[3] <= parent.height
        ):
            raise ValueError("Evidence crop bounds are outside their parent")
        content = image_content(parent.crop(bounds))
    elif path.is_file():
        content = path.read_bytes()
    else:
        raise ValueError("Evidence image is unavailable; use its original timestamp link")
    if len(content) != asset["bytes"] or hashlib.sha256(content).hexdigest() != asset["sha256"]:
        raise ValueError("Evidence image is missing or corrupt (checksum mismatch)")
    return content


def read_image(root, asset):
    with Image.open(io.BytesIO(read_image_bytes(root, asset))) as image:
        if image.size != (asset["width"], asset["height"]):
            raise ValueError("Evidence image dimensions disagree with its manifest")
        return image.convert("RGB")
