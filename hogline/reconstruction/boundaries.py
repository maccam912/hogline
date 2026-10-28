"""Propose nearby recording gaps; canonical game membership stays independent."""

from bisect import bisect_left, bisect_right
from collections import defaultdict

from hogline.adapters.sources import digest
from hogline.media.clocks import fit_clock_hypotheses

VERSION = "clock-board-boundary-links-v1"


def stable_board_rows(observations):
    rows = {}
    for color in ("red", "yellow"):
        votes = defaultdict(dict)
        for observation in observations:
            board = observation.get("scoreboard", {})
            if board.get("status") != "observed" or board.get("layout_alignment", {}).get("status") not in (
                "compatible_seed",
                "translated_seed",
            ):
                continue
            cards = [c for c in board.get("cards", []) if c["color"] == color]
            if len(cards) < 2 or any(c.get("end_number") is None for c in cards):
                continue
            if any(s["color"] == color for s in board.get("unreadable_slots", [])):
                continue
            if any(c["end_number"] in board.get("conflicting_end_numbers", []) for c in cards):
                continue
            key = tuple(sorted((c["end_number"], c["cumulative_points"]) for c in cards))
            votes[key].setdefault(round(observation["source_seconds"], 6), observation["evidence_id"])
        supported = [(key, ids) for key, ids in votes.items() if len(ids) >= 2]
        if len(supported) == 1:
            key, samples = supported[0]
            rows[color] = {
                "cards": [{"end_number": end, "cumulative_points": points} for end, points in key],
                "evidence_ids": sorted(samples.values()),
            }
    return rows


def board_comparison(left, right):
    a, b = stable_board_rows(left), stable_board_rows(right)
    agreeing = [c for c in a.keys() & b.keys() if a[c]["cards"] == b[c]["cards"]]
    changed = [c for c in a.keys() & b.keys() if a[c]["cards"] != b[c]["cards"]]
    return {
        "status": "concordant_readable_rows"
        if agreeing
        else "changed_readable_rows"
        if changed
        else "insufficient_readable_rows",
        "agreeing_colors": sorted(agreeing),
        "changed_colors": sorted(changed),
        "left": a,
        "right": b,
        "interpretation": "posted_board_state_only_not_independent_game_or_result_proof",
    }


def boundary_links(
    sources, observations, broadcast_version, evidence_ids=None, maximum_gap=600, maximum_extrapolation=120
):
    source_map = {s["id"]: s for s in sources}
    physical_samples = {}
    for observation in observations:
        source = source_map.get(observation["source_id"])
        if not source or not source.get("sheet") or not source.get("metadata_sha256"):
            continue
        if observation.get("source_metadata_sha256") != source["metadata_sha256"]:
            continue
        if evidence_ids is not None and observation["evidence_id"] not in evidence_ids:
            continue
        physical_samples.setdefault(
            (observation["source_id"], round(observation["source_seconds"], 6)), observation
        )
    eligible = list(physical_samples.values())
    # Repeated OCR alternatives never count as additional physical samples.
    hypotheses = fit_clock_hypotheses(eligible)
    by_evidence = {o["evidence_id"]: o for o in eligible}
    starts = defaultdict(list)
    for h in hypotheses:
        if h["source_start_seconds"] <= maximum_extrapolation:
            starts[source_map[h["source_id"]]["sheet"]].append(h)
    for group in starts.values():
        group.sort(key=lambda h: h["wall_offset_seconds"])
    result = {}
    for left in hypotheses:
        source = source_map[left["source_id"]]
        duration = source.get("duration_seconds")
        if duration is None or not 0 <= duration - left["source_end_seconds"] <= maximum_extrapolation:
            continue
        rights = starts[source["sheet"]]
        offsets = [h["wall_offset_seconds"] for h in rights]
        end_epoch = left["wall_offset_seconds"] + duration
        for right in rights[bisect_left(offsets, end_epoch) : bisect_right(offsets, end_epoch + maximum_gap)]:
            if right["source_id"] == left["source_id"]:
                continue
            other = source_map[right["source_id"]]
            gap = right["wall_offset_seconds"] - end_epoch
            a = [by_evidence[i] for i in left["evidence_ids"]]
            b = [by_evidence[i] for i in right["evidence_ids"]]
            nearest_left = max(a, key=lambda o: o["source_seconds"])
            nearest_right = min(b, key=lambda o: o["source_seconds"])
            board = board_comparison(a, b)
            key = (source["id"], other["id"], round(gap))
            option = {
                "left_wall_offset_seconds": left["wall_offset_seconds"],
                "right_wall_offset_seconds": right["wall_offset_seconds"],
            }
            if key in result:
                if option not in result[key]["clock_alternatives"]:
                    result[key]["clock_alternatives"].append(option)
                continue
            proposal = {
                "left_source_id": source["id"],
                "right_source_id": other["id"],
                "sheet": source["sheet"],
                "kind": "possible_continuation" if board["agreeing_colors"] else "nearby_recording_gap",
                "acceptance": "proposed",
                "model_version": VERSION,
                "broadcast_version": broadcast_version,
                "estimated_recording_gap_seconds": gap,
                "gap_uncertainty_seconds": left["uncertainty_seconds"] + right["uncertainty_seconds"] + 1,
                "gap_uncertainty_kind": "conditional_on_clock_continuity_and_declared_duration",
                "unobserved_between_nearest_anchors_seconds": right["wall_offset_seconds"]
                + nearest_right["source_seconds"]
                - left["wall_offset_seconds"]
                - nearest_left["source_seconds"],
                "boundary_extrapolation_seconds": {
                    "left": duration - left["source_end_seconds"],
                    "right": right["source_start_seconds"],
                },
                "left": {
                    "source_start_seconds": left["source_start_seconds"],
                    "source_end_seconds": left["source_end_seconds"],
                    "nearest_evidence_id": nearest_left["evidence_id"],
                    "youtube_url": nearest_left.get("youtube_url"),
                    "evidence_ids": left["evidence_ids"],
                },
                "right": {
                    "source_start_seconds": right["source_start_seconds"],
                    "source_end_seconds": right["source_end_seconds"],
                    "nearest_evidence_id": nearest_right["evidence_id"],
                    "youtube_url": nearest_right.get("youtube_url"),
                    "evidence_ids": right["evidence_ids"],
                },
                "source_manifest_hashes": {
                    source["id"]: source["metadata_sha256"],
                    other["id"]: other["metadata_sha256"],
                },
                "clock_alternatives": [option],
                "board_comparison": board,
                "missing_footage": "deliveries_end_boundaries_and_clock_changes_inside_the_gap_are_unknown",
                "game_count": None,
                "result": None,
            }
            proposal["id"] = digest({k: v for k, v in proposal.items() if k != "clock_alternatives"})[:24]
            result[key] = proposal
    return sorted(
        result.values(), key=lambda p: (p["sheet"], p["left_source_id"], p["estimated_recording_gap_seconds"])
    )
