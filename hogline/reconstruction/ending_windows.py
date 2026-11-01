"""Bounded inspection candidates near every board reset and the recording tail."""

import math

VERSION = "board-ending-context-v1"


def ending_windows(report, duration, limit):
    if not math.isfinite(duration) or duration <= 0 or not 1 <= limit <= 32:
        raise ValueError("Ending inspection requires a finite duration and one to 32 windows")
    groups = []
    for book in report.get("scorecards", []):
        if not book.get("ends"):
            continue
        reset = book.get("after_reset_epoch")
        boundary = min(duration, reset["first_seconds"] if reset else duration)
        candidates = []
        # Posting/reset can lag the final delivery by several minutes. Cover eight
        # preceding minutes, closest first, instead of treating the tail as the game.
        for offset in range(8):
            end = max(0, boundary - offset * 60)
            start = max(0, end - 60, book.get("first_seconds", 0))
            if end - start < 2:
                continue
            candidates.append(
                {
                    "start_seconds": start,
                    "end_seconds": end,
                    "history_id": book["id"],
                    "reason": "before_board_reset" if reset else "recording_tail",
                    "boundary_seconds": boundary,
                    "evidence_ids": list(reset.get("evidence_ids", [])) if reset else [],
                }
            )
        groups.append(candidates)
    output, seen = [], set()
    # Round-robin means an early game cannot consume every window in a long stream.
    for depth in range(8):
        for group in groups:
            if depth >= len(group):
                continue
            candidate = group[depth]
            key = (candidate["start_seconds"], candidate["end_seconds"])
            if key in seen:
                continue
            seen.add(key)
            output.append(candidate)
    return {
        "version": VERSION,
        "windows": output[:limit],
        "deferred_windows": max(0, len(output) - limit),
        "notice": "Inspection candidates only; board resets and recording ends do not prove game completion.",
    }
