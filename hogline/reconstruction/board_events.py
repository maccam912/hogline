"""Evidence brackets for changes in posted cards, never played-end timestamps."""

import math
from collections import defaultdict
from itertools import groupby
from uuid import NAMESPACE_URL, uuid5

from hogline.adapters.sources import digest

VERSION = "posted-board-change-brackets-v1"


def row_reading(observation, color):
    board = observation.get("scoreboard", {})
    if board.get("status") != "observed":
        return None
    cards = [c for c in board.get("cards", []) if c.get("color") == color]
    if any(s.get("color") == color for s in board.get("unreadable_slots", [])):
        return None
    values = []
    for card in cards:
        end, points = card.get("end_number"), card.get("cumulative_points")
        if (
            not isinstance(end, int)
            or isinstance(end, bool)
            or not 1 <= end <= 16
            or not isinstance(points, int)
            or isinstance(points, bool)
            or not 1 <= points <= 15
            or end in board.get("conflicting_end_numbers", [])
        ):
            return None
        values.append((end, points))
    if len({end for end, _ in values}) != len(values):
        return None
    # Empty is only "no readable cards", never a zero score or proof of clear visibility.
    return tuple(sorted(values))


def anchor(observation):
    return {
        "observation_id": observation["id"],
        "evidence_id": observation["evidence_id"],
        "source_seconds": observation["source_seconds"],
        "youtube_url": observation.get("youtube_url"),
    }


def board_timeline(observations, minimum_frames=3, maximum_gap_seconds=90, minimum_span_seconds=2):
    if (
        minimum_frames < 3
        or not math.isfinite(maximum_gap_seconds)
        or maximum_gap_seconds <= 0
        or not math.isfinite(minimum_span_seconds)
        or minimum_span_seconds <= 0
    ):
        raise ValueError("Board timelines need at least three observations and finite positive time limits")
    partitions = defaultdict(list)
    excluded = 0
    for o in observations:
        seconds = o.get("source_seconds")
        if (
            not isinstance(seconds, (int, float))
            or not math.isfinite(seconds)
            or seconds < 0
            or not o.get("id")
            or not o.get("evidence_id")
        ):
            continue
        if not o.get("source_metadata_sha256") or not o.get("model_version"):
            excluded += 1
            continue
        # Metadata and model boundaries cannot be crossed by an event hypothesis.
        partitions[(o["source_id"], o.get("source_metadata_sha256"), o.get("model_version"))].append(o)
    epochs, changes = [], []
    for partition, source_observations in sorted(partitions.items(), key=lambda item: str(item[0])):
        ordered = sorted(source_observations, key=lambda o: (o["source_seconds"], o["id"]))
        for color in ("red", "yellow"):
            samples, seen_evidence = [], set()
            for seconds, same_time in groupby(ordered, key=lambda o: o["source_seconds"]):
                alternatives = list(same_time)
                readings = {row_reading(o, color) for o in alternatives}
                selected = alternatives[0]
                # Alternative crops/uploads at the same source time do not add votes.
                value = next(iter(readings)) if len(readings) == 1 else None
                if selected["evidence_id"] in seen_evidence:
                    value = None
                seen_evidence.update(o["evidence_id"] for o in alternatives)
                samples.append({"observation": selected, "reading": value, "seconds": seconds})
            groups, current = [], None
            for sample in samples:
                if sample["reading"] is None:
                    current = None
                    continue
                if (
                    current is None
                    or current[-1]["reading"] != sample["reading"]
                    or sample["seconds"] - current[-1]["seconds"] > maximum_gap_seconds
                ):
                    current = []
                    groups.append(current)
                current.append(sample)
            stable = []
            for group in groups:
                if (
                    len(group) < minimum_frames
                    or group[-1]["seconds"] - group[0]["seconds"] < minimum_span_seconds
                ):
                    continue
                readings = group[0]["reading"]
                epoch = {
                    "kind": "posted_board_epoch",
                    "version": VERSION,
                    "source_id": partition[0],
                    "source_metadata_sha256": partition[1],
                    "model_version": partition[2],
                    "color": color,
                    "cards": [{"end_number": end, "cumulative_points": points} for end, points in readings],
                    "reading_kind": "readable_cards" if readings else "no_readable_cards",
                    "first": anchor(group[0]["observation"]),
                    "last": anchor(group[-1]["observation"]),
                    "evidence_ids": [s["observation"]["evidence_id"] for s in group],
                    "observation_ids": [s["observation"]["id"] for s in group],
                    "acceptance": "proposed",
                    "score": None,
                }
                epoch["id"] = str(uuid5(NAMESPACE_URL, digest(epoch)))
                stable.append(epoch)
                epochs.append(epoch)
            for before, after in zip(stable, stable[1:], strict=False):
                old = {c["end_number"]: c["cumulative_points"] for c in before["cards"]}
                new = {c["end_number"]: c["cumulative_points"] for c in after["cards"]}
                if old == new:
                    continue
                added = [
                    {"end_number": n, "cumulative_points": new[n]} for n in sorted(new.keys() - old.keys())
                ]
                removed = [
                    {"end_number": n, "cumulative_points": old[n]} for n in sorted(old.keys() - new.keys())
                ]
                moved = [
                    {"end_number": n, "before_column": old[n], "after_column": new[n]}
                    for n in sorted(old.keys() & new.keys())
                    if old[n] != new[n]
                ]
                left, right = before["last"], after["first"]
                between = [
                    s for s in samples if left["source_seconds"] < s["seconds"] < right["source_seconds"]
                ]
                invalid = sum(s["reading"] is None for s in between)
                gap = right["source_seconds"] - left["source_seconds"]
                doc = {
                    "kind": "posted_board_change",
                    "version": VERSION,
                    "source_id": partition[0],
                    "source_metadata_sha256": partition[1],
                    "model_version": partition[2],
                    "color": color,
                    "before_epoch_id": before["id"],
                    "after_epoch_id": after["id"],
                    "before": left,
                    "after": right,
                    "bracket_seconds": gap,
                    "intervening_readings": len(between),
                    "unusable_intervening_readings": invalid,
                    "continuity": "interrupted_readings"
                    if invalid
                    else "sparse_gap"
                    if gap > maximum_gap_seconds
                    else "sampled_only",
                    "added_cards": added,
                    "removed_cards": removed,
                    "moved_cards": moved,
                    "change_type": "no_readable_cards"
                    if not new
                    else "cards_appeared"
                    if not old
                    else "cards_added"
                    if added and not removed and not moved
                    else "cards_removed"
                    if removed and not added and not moved
                    else "cards_repositioned"
                    if moved and not added and not removed
                    else "mixed_card_change",
                    "evidence_ids": list(dict.fromkeys(before["evidence_ids"] + after["evidence_ids"])),
                    "acceptance": "proposed",
                    "score": None,
                    "game_id": None,
                    "event_time_seconds": None,
                    "played_end_time_seconds": None,
                    "review_reason": "posted_cards_can_lag_play_or_be_obscured_corrected_or_reset",
                }
                doc["id"] = str(uuid5(NAMESPACE_URL, digest(doc)))
                changes.append(doc)
    return {
        "version": VERSION,
        "epochs": epochs,
        "changes": changes,
        "minimum_frames": minimum_frames,
        "minimum_span_seconds": minimum_span_seconds,
        "maximum_gap_seconds": maximum_gap_seconds,
        "excluded_unpinned_observations": excluded,
        "notice": (
            "Changes bracket repeated card readings, not end completion. "
            "Missing cards do not establish a zero score or a new game."
        ),
    }
