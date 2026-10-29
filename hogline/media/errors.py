"""Transport failures may be retried; invalid evidence must still fail explicitly."""

import subprocess

_bounded_requests = False


def configure_media_transport():
    """Use bounded HTTP byte ranges only when both installed media tools support them."""
    global _bounded_requests
    supported = []
    for tool in ("ffmpeg", "ffprobe"):
        try:
            result = subprocess.run(
                [tool, "-hide_banner", "-h", "protocol=http"],
                capture_output=True,
                text=True,
                check=True,
                timeout=5,
            )
            supported.append(
                all(
                    flag in result.stdout
                    for flag in ("-request_size", "-initial_request_size", "-multiple_requests")
                )
            )
        except (OSError, subprocess.SubprocessError):
            supported.append(False)
    _bounded_requests = all(supported)
    return {"bounded_http_ranges": _bounded_requests, "request_bytes": 1024**2 if _bounded_requests else None}


class TransientMediaError(ValueError):
    pass


def remote_options(media):
    if not media.startswith("https://"):
        return []
    options = [
        "-rw_timeout",
        "15000000",
        "-reconnect",
        "1",
        "-reconnect_streamed",
        "1",
        "-reconnect_on_network_error",
        "1",
        "-reconnect_on_http_error",
        "429,5xx",
        "-reconnect_delay_max",
        "2",
    ]
    if _bounded_requests:
        options += [
            "-request_size",
            str(1024**2),
            "-initial_request_size",
            str(1024**2),
            "-multiple_requests",
            "1",
        ]
    return options
