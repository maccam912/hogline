"""Clock hypotheses: overlays are evidence, never a timestamp oracle."""

import re
from collections import defaultdict
from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo


def parse_overlay(date_text, time_text, timezone="America/Chicago"):
    date_match = re.search(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{4})\b", date_text)
    time_match = re.search(r"\b(\d{1,2}):(\d{2}):(\d{2})\s*(AM|PM)?\b", time_text, re.I)
    result = {
        "date_text": date_text,
        "time_text": time_text,
        "timezone": timezone,
        "acceptance": "proposed",
        "local_date": None,
        "candidates": [],
        "uncertainty_seconds": 1,
        "missing_reason": None,
    }
    if not date_match or not time_match:
        result["missing_reason"] = "overlay_date_or_time_not_read"
        return result
    try:
        day = date(int(date_match[3]), int(date_match[1]), int(date_match[2]))
        hour, minute, second = map(int, time_match.group(1, 2, 3))
        if minute >= 60 or second >= 60 or hour > 23:
            raise ValueError("Invalid clock")
    except ValueError:
        result["missing_reason"] = "invalid_overlay_clock"
        return result
    result["local_date"] = day.isoformat()
    marker = time_match[4]
    if marker:
        if not 1 <= hour <= 12:
            result["missing_reason"] = "invalid_12_hour_clock"
            return result
        hours = [hour % 12 + (12 if marker.upper() == "PM" else 0)]
    elif 1 <= hour <= 12:
        hours = [hour % 12, hour % 12 + 12]
    else:
        hours = [hour]
    zone = ZoneInfo(timezone)
    for value in hours:
        naive = datetime.combine(day, time(value, minute, second))
        seen = set()
        for fold in (0, 1):
            local = naive.replace(tzinfo=zone, fold=fold)
            utc = local.astimezone(UTC)
            if utc.astimezone(zone).replace(tzinfo=None) != naive or utc in seen:
                continue  # Reject nonexistent spring-forward times; retain both fall-back alternatives.
            seen.add(utc)
            result["candidates"].append({"local": local.isoformat(), "utc": utc.isoformat(), "fold": fold})
    if not result["candidates"]:
        result["missing_reason"] = "nonexistent_local_time"
    result["ambiguous"] = len(result["candidates"]) != 1
    return result


def fit_clock_hypotheses(observations, tolerance_seconds=2, minimum_samples=3, maximum_gap_seconds=120):
    """Cluster PTS-to-wall offsets without choosing AM/PM from an upload date.

    Each offset cluster is a mapping hypothesis, not a confirmed clock segment.
    Gaps/jumps retain separate clusters; duplicate evidence cannot add support.
    """
    groups = defaultdict(list)
    for observation in observations:
        groups[observation["source_id"]].append(observation)
    output = []
    for source_id, frames in groups.items():
        clusters = []
        for index, frame in enumerate(sorted(frames, key=lambda f: f["source_seconds"])):
            for candidate in frame.get("clock", {}).get("candidates", []):
                offset = datetime.fromisoformat(candidate["utc"]).timestamp() - frame["source_seconds"]
                match = next(
                    (
                        c
                        for c in clusters
                        if c["last_index"] == index - 1
                        and abs(c["values"][0][0] - offset) <= tolerance_seconds
                        and frame["source_seconds"] - c["values"][-1][2] <= maximum_gap_seconds
                    ),
                    None,
                )
                if match is None:
                    match = {"last_index": index, "values": []}
                    clusters.append(match)
                match["values"].append((offset, frame["evidence_id"], frame["source_seconds"]))
                match["last_index"] = index
        for group in clusters:
            cluster = group["values"]
            ids = sorted({v[1] for v in cluster})
            if len(ids) < minimum_samples or len({v[2] for v in cluster}) < minimum_samples:
                continue
            values = [v[0] for v in cluster]
            output.append(
                {
                    "source_id": source_id,
                    "wall_offset_seconds": sum(values) / len(values),
                    "uncertainty_seconds": 1 + (max(values) - min(values)) / 2,
                    "source_start_seconds": min(v[2] for v in cluster),
                    "source_end_seconds": max(v[2] for v in cluster),
                    "evidence_ids": ids,
                    "acceptance": "proposed",
                    "method": "consecutive_overlay_offset_segment_v2",
                }
            )
    return output
