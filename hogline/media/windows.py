"""Decode a bounded interval once without replacing original presentation times."""

import io
import math
import os
import re
import selectors
import struct
import subprocess
import time
from collections import deque

from PIL import Image

VERSION = "original-pts-window-v1"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_FRAME_BYTES = 32 * 1024**2
MAX_PIXELS = 4096 * 2160


def pop_png(buffer):
    """Consume one complete PNG; refuse malformed or unexpectedly large output."""
    if len(buffer) < 8:
        return None
    if buffer[:8] != PNG_SIGNATURE:
        raise ValueError("Window decoder returned invalid image framing")
    offset = 8
    while len(buffer) >= offset + 8:
        length = struct.unpack_from(">I", buffer, offset)[0]
        end = offset + 12 + length
        if end > MAX_FRAME_BYTES:
            raise ValueError("Decoded frame exceeds the bounded image buffer")
        if len(buffer) < end:
            return None
        if buffer[offset + 4 : offset + 8] == b"IEND":
            content = bytes(buffer[:end])
            del buffer[:end]
            return content
        offset = end
    return None


def decode_window(media, start, end, interval=0.5, max_frames=120, timeout=90, tick=None):
    """Yield selected PNGs and showinfo PTS from one FFmpeg process.

    The interval is a minimum separation between source frames, not a promise
    of constant frame rate. No fps/setpts filter or inferred frame-number time.
    Closing the generator, cancellation, and timeouts all terminate the decoder.
    """
    if (
        not all(math.isfinite(v) for v in (start, end, interval, timeout))
        or not 0 <= start < end <= start + 60
        or not 0.25 <= interval <= 2
        or not 1 <= max_frames <= 240
        or math.ceil((end - start) / interval) > max_frames
        or not 1 <= timeout <= 120
    ):
        raise ValueError("Invalid bounded decoding window or frame/time budget")
    command = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "info"]
    if media.startswith("https://"):
        command += ["-rw_timeout", "15000000"]
    command += [
        "-copyts",
        "-ss",
        str(start),
        "-t",
        str(end - start),
        "-i",
        media,
        "-map",
        "0:v:0",
        "-vf",
        f"select='isnan(prev_selected_t)+gte(t-prev_selected_t,{interval - 1e-7:.9f})',showinfo",
        "-frames:v",
        str(max_frames + 1),
        "-an",
        "-fps_mode",
        "passthrough",
        "-f",
        "image2pipe",
        "-c:v",
        "png",
        "pipe:1",
    ]
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError("The bounded video decoder could not start") from exc
    started = time.monotonic()
    pixels, log = bytearray(), bytearray()
    images, timings = deque(), deque()
    timebase, count, seen, previous_pts = None, 0, 0, None
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, "image")
            selector.register(process.stderr, selectors.EVENT_READ, "log")
            while selector.get_map():
                if time.monotonic() - started > timeout:
                    raise ValueError("Bounded video decoding timed out; no complete window was published")
                if tick:
                    tick()
                for key, _ in selector.select(timeout=0.25):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    if key.data == "image":
                        pixels.extend(chunk)
                        while (png := pop_png(pixels)) is not None:
                            images.append(png)
                    else:
                        log.extend(chunk)
                        while b"\n" in log:
                            line, _, rest = log.partition(b"\n")
                            log = bytearray(rest)
                            text = line.decode(errors="replace")
                            tb = re.search(r"config in time_base:\s*(\d+)/(\d+)", text)
                            if tb:
                                new_timebase = (int(tb[1]), int(tb[2]))
                                if timebase and timebase != new_timebase:
                                    raise ValueError("Source timebase changed inside the analysis window")
                                timebase = new_timebase
                            match = re.search(r"n:\s*(\d+)\s+pts:\s*(-?\d+)\s+pts_time:", text)
                            if match:
                                if not timebase or int(match[1]) != seen or not all(timebase):
                                    raise ValueError("Window decoder returned ambiguous original timestamps")
                                pts = int(match[2])
                                if previous_pts is not None and pts <= previous_pts:
                                    raise ValueError("Source presentation timestamps are not increasing")
                                previous_pts, seen = pts, seen + 1
                                timings.append(
                                    {"pts": pts, "time_base_num": timebase[0], "time_base_den": timebase[1]}
                                )
                    if len(pixels) + sum(map(len, images)) > MAX_FRAME_BYTES * 2 or len(log) > 1024**2:
                        raise ValueError("Window decoder exceeded its bounded output buffer")
                    while images and timings:
                        count += 1
                        if count > max_frames:
                            raise ValueError("Window decoder exceeded the declared frame budget")
                        with Image.open(io.BytesIO(images.popleft())) as decoded:
                            if decoded.width * decoded.height > MAX_PIXELS:
                                raise ValueError("Decoded resolution exceeds the supported analysis size")
                            image = decoded.convert("RGB")
                        yield image, timings.popleft()
            code = process.wait(timeout=1)
            if code or pixels or images or timings or not count:
                raise ValueError("Window decoding was incomplete; no complete window was published")
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        process.stdout.close()
        process.stderr.close()
