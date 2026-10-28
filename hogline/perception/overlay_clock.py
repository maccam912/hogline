"""Retain competing clock reads; never repair a date from a recording title."""

from hogline.media.clocks import parse_overlay


def combine_clock_reads(date_reads, time_reads, timezone):
    alternatives = []
    for di, date in enumerate(date_reads):
        for ti, time in enumerate(time_reads):
            parsed = parse_overlay(date.get("text", ""), time.get("text", ""), timezone)
            if not parsed["candidates"]:
                continue
            key = tuple(c["utc"] for c in parsed["candidates"])
            existing = next(
                (a for a in alternatives if tuple(c["utc"] for c in a["candidates"]) == key), None
            )
            if existing:
                existing["supporting_reads"].append({"date_read": di, "time_read": ti})
            else:
                alternatives.append({**parsed, "supporting_reads": [{"date_read": di, "time_read": ti}]})
    base = parse_overlay(date_reads[0].get("text", ""), time_reads[0].get("text", ""), timezone)
    candidates = {}
    for index, alternative in enumerate(alternatives):
        for candidate in alternative["candidates"]:
            entry = candidates.setdefault(candidate["utc"], {**candidate, "reading_alternatives": []})
            entry["reading_alternatives"].append(index)
    if alternatives:
        dates = {a["local_date"] for a in alternatives}
        times = {tuple(c["local"].split("T")[1] for c in a["candidates"]) for a in alternatives}
        base = {
            **alternatives[0],
            "local_date": next(iter(dates)) if len(dates) == 1 else None,
            "date_text": alternatives[0]["date_text"] if len(dates) == 1 else "",
            "time_text": alternatives[0]["time_text"] if len(times) == 1 else "",
            "missing_reason": None,
        }
        base.pop("supporting_reads", None)
    return {
        **base,
        "candidates": list(candidates.values()),
        "alternatives": alternatives,
        "ambiguous": len(candidates) != 1,
        "ocr_conflict": len(alternatives) > 1,
        "date_reads": date_reads,
        "time_reads": time_reads,
        "method": "multi_clock_crop_preprocessing_v3",
    }


def read_clock(date_image, time_image, reader, timezone):
    # The secondary date crop removes 5% of the seed ROI's left margin. It is a
    # separate retained reading, so clipping/misreads cannot erase the primary.
    trim = round(date_image.width * 0.05)
    tight = date_image.crop((trim, 0, date_image.width, date_image.height))
    date_reads = [
        {**reader.read(date_image, whitelist="0123456789/", psm=13), "crop": "clock_date"},
        {
            **reader.read(tight, whitelist="0123456789/", psm=13, preprocessing="gray"),
            "crop": "clock_date_tight",
        },
        {
            **reader.read(date_image, whitelist="0123456789/", psm=7, preprocessing="gray"),
            "crop": "clock_date",
        },
    ]
    time_reads = [
        {**reader.read(time_image, whitelist="0123456789:APM", psm=7), "crop": "clock_time"},
        {
            **reader.read(time_image, whitelist="0123456789:APM", psm=13, preprocessing="gray"),
            "crop": "clock_time",
        },
    ]
    time_reads.append(
        {
            **reader.read(time_image, whitelist="0123456789:APM", psm=7, preprocessing="gray"),
            "crop": "clock_time",
        }
    )
    result = combine_clock_reads(date_reads, time_reads, timezone)
    result["date_tight_crop_transform"] = {"parent": "clock_date", "left_px": trim}
    return result, tight
