"""Transport failures may be retried; invalid evidence must still fail explicitly."""


class TransientMediaError(ValueError):
    pass


def remote_options(media):
    if not media.startswith("https://"):
        return []
    return [
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
