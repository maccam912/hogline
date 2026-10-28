"""Combine sparse work-unit coverage without treating chunks as games."""

from hogline.adapters.sources import digest, video_link


def union_intervals(intervals):
    merged = []
    for start, end in sorted(intervals):
        if end <= start:
            continue
        if merged and start <= merged[-1][1] + 1e-6:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


def covers(intervals, start, end):
    return any(a <= start + 1e-6 and b >= end - 1e-6 for a, b in intervals)


def reconstruct_source(source, scans, evidence, quiet_gap=300):
    samples, coverage = {}, []
    stale = 0
    for scan in scans:
        if scan.get("source_metadata_sha256", source.get("metadata_sha256")) != source.get("metadata_sha256"):
            stale += 1
            continue
        request = scan["request"]
        start = request.get("ownership_start_seconds")
        end = request.get("ownership_end_seconds")
        start = request["start_seconds"] if start is None else start
        end = request["end_seconds"] if end is None else end
        coverage.append((start, end))
        for sample in scan["samples"]:
            if not sample.get("owned", start <= sample["seconds"] < end):
                continue
            frame = evidence.get(sample["evidence_id"])
            if frame is None:
                continue
            key = round(sample.get("requested_seconds", sample["seconds"]), 6)
            asset = frame["image"]
            quality = (
                asset["width"] * asset["height"],
                frame.get("acquisition_tier", "evidence") == "evidence",
            )
            previous = samples.get(key)
            if previous is None or quality > previous["quality"]:
                samples[key] = {
                    **sample,
                    "interval": request["interval_seconds"],
                    "quality": quality,
                    "owner_start": start,
                    "owner_end": end,
                }
    coverage = union_intervals(coverage)
    runs = []
    current = None
    previous = None
    for sample in sorted(samples.values(), key=lambda s: s["seconds"]):
        if previous and (
            sample["seconds"] - previous["seconds"]
            > max(2 * max(sample["interval"], previous["interval"]), 30)
            or not covers(coverage, previous["seconds"], sample["seconds"])
        ):
            current = None
        if sample["state"] == "dark_candidate":
            current = None
        elif sample["state"] == "activity_candidate":
            if current is None or sample["seconds"] - current["last_active_seconds"] > quiet_gap:
                current = {
                    "source_id": source["id"],
                    "start_seconds": max(sample["owner_start"], sample["seconds"] - sample["interval"]),
                    "end_seconds": min(sample["owner_end"], sample["seconds"] + sample["interval"]),
                    "last_active_seconds": sample["seconds"],
                    "evidence_ids": [],
                    "boundary_uncertainty_seconds": sample["interval"],
                    "kind": "activity_run",
                    "acceptance": "proposed",
                    "interpretation": "motion_only_not_a_game_boundary",
                }
                runs.append(current)
            current["end_seconds"] = min(sample["owner_end"], sample["seconds"] + sample["interval"])
            current["last_active_seconds"] = sample["seconds"]
            if sample["evidence_id"] not in current["evidence_ids"]:
                current["evidence_ids"].append(sample["evidence_id"])
            current["boundary_uncertainty_seconds"] = max(
                current["boundary_uncertainty_seconds"], sample["interval"]
            )
        previous = sample
    for run in runs:
        run["youtube_url"] = video_link(source, run["start_seconds"])
        run["id"] = digest({k: v for k, v in run.items() if k != "last_active_seconds"})[:24]
    duration = source.get("duration_seconds")
    missing = None
    if duration is not None:
        missing = []
        cursor = 0
        for a, b in coverage:
            if a > cursor:
                missing.append([cursor, min(a, duration)])
            cursor = max(cursor, min(b, duration))
        if cursor < duration:
            missing.append([cursor, duration])
    return {
        "source_id": source["id"],
        "sheet": source.get("sheet"),
        "duration_seconds": duration,
        "sparse_scanned_intervals": coverage,
        "sparse_scanned_seconds": sum(b - a for a, b in coverage),
        "unique_owned_samples": len(samples),
        "stale_scans_excluded": stale,
        "unscanned_intervals": missing,
        "activity_runs": runs,
        "coverage_kind": "requested_spans_with_sparse_samples_not_continuous_observability",
        "game_count": None,
    }
