"""Reconcile independent card sightings into separate, source-linked score histories.

Rows need not be simultaneously readable. Unknown cards never become blank ends;
confirmed empty-board epochs provide segmentation evidence, not physical play times.
"""

import math
from bisect import bisect_right
from collections import defaultdict
from uuid import NAMESPACE_URL, uuid5

from hogline.adapters.sources import digest
from hogline.domain.provenance import observation_manifest
from hogline.reconstruction.ends import paired_epochs

VERSION = "temporal-scorecards-v2"
COLORS = ("red", "yellow")


def identified(document):
    return {"id": str(uuid5(NAMESPACE_URL, digest(document))), **document}


def anchor(observation):
    return {
        "observation_id": observation["id"],
        "evidence_id": observation["evidence_id"],
        "source_seconds": observation["source_seconds"],
        "youtube_url": observation.get("youtube_url"),
    }


def valid_integer(value, lo, hi):
    return isinstance(value, int) and not isinstance(value, bool) and lo <= value <= hi


def visible_claims(observation):
    board = observation.get("scoreboard", {})
    if board.get("status") != "observed":
        return set()
    claims = {
        (card["end_number"], card["color"], card["cumulative_points"])
        for card in board.get("cards", [])
        if card.get("color") in COLORS
        and valid_integer(card.get("end_number"), 1, 16)
        and valid_integer(card.get("cumulative_points"), 1, 15)
    }
    for card in board.get("blank_end_cards", []):
        if valid_integer(card.get("end_number"), 1, 16):
            claims.add((card["end_number"], "blank", 0))
    # A single frame assigning the same end to two slots cannot vote for either.
    conflicts = {n for n, _, _ in claims if sum(c[0] == n for c in claims) > 1}
    return {claim for claim in claims if claim[0] not in conflicts}


def stable_claims(observations, minimum_frames=3, minimum_span=2):
    votes = defaultdict(dict)
    for observation in observations:
        for claim in visible_claims(observation):
            votes[claim][observation["evidence_id"]] = observation
    candidates = defaultdict(list)
    for (number, color, total), evidence in votes.items():
        samples = sorted(evidence.values(), key=lambda o: (o["source_seconds"], o["id"]))
        if (
            len(samples) < minimum_frames
            or samples[-1]["source_seconds"] - samples[0]["source_seconds"] < minimum_span
        ):
            continue
        candidates[number].append(
            {
                "official_number": number,
                "color": color,
                "cumulative_points": total,
                "supporting_frames": len(samples),
                "first": anchor(samples[0]),
                "last": anchor(samples[-1]),
                "evidence_ids": [o["evidence_id"] for o in samples],
                "observation_ids": [o["id"] for o in samples],
            }
        )
    selected, conflicts = {}, []
    for number, alternatives in sorted(candidates.items()):
        alternatives.sort(key=lambda c: (-c["supporting_frames"], c["color"], c["cumulative_points"]))
        best = alternatives[0]
        if len(alternatives) > 1 and alternatives[1]["supporting_frames"] >= max(
            3, best["supporting_frames"] * 0.25
        ):
            conflicts.append(
                {
                    "official_number": number,
                    "reason": "competing_repeated_card_readings",
                    "alternatives": alternatives,
                }
            )
            continue
        selected[number] = {**best, "competing_readings": alternatives[1:]}
    return selected, conflicts


def score_rows(claims):
    totals = {color: 0 for color in COLORS}
    ends = []
    highest = max(claims, default=0)
    for number in range(1, highest + 1):
        claim = claims.get(number)
        points = {color: None for color in COLORS}
        reasons = []
        if claim is None:
            totals = {color: None for color in COLORS}
            reasons.append("end_card_missing_or_conflicting; blank_not_inferred")
        elif claim["color"] == "blank":
            points = {color: 0 for color in COLORS}
        else:
            color = claim["color"]
            other = "yellow" if color == "red" else "red"
            previous = totals[color]
            difference = claim["cumulative_points"] - previous if previous is not None else None
            if difference is not None and not 1 <= difference <= 8:
                reasons.append("cumulative_score_conflicts_with_prior_end")
                totals = {color: None for color in COLORS}
            else:
                points[color], points[other] = difference, 0
                totals[color] = claim["cumulative_points"]
                if difference is None:
                    reasons.append("previous_cumulative_total_unresolved")
        ends.append(
            {
                "official_number": number,
                "points": dict(points),
                "score_after_end": dict(totals),
                "state": "blank"
                if claim and claim["color"] == "blank"
                else "played"
                if all(v is not None for v in points.values())
                else "unknown",
                "status": "proposed" if all(v is not None for v in points.values()) else "unresolved",
                "board_evidence": claim,
                "unknown_reasons": reasons,
                "first_delivery": None,
                "last_stone_stopped": None,
                "score_decided": None,
            }
        )
    return ends


def current_end_evidence(observations):
    votes = defaultdict(dict)
    for observation in observations:
        number = (observation.get("scoreboard", {}).get("current_end") or {}).get("number")
        if valid_integer(number, 1, 16):
            votes[number][observation["evidence_id"]] = observation
    output = []
    for number, evidence in sorted(votes.items()):
        samples = sorted(evidence.values(), key=lambda o: o["source_seconds"])
        if len(samples) >= 3 and samples[-1]["source_seconds"] - samples[0]["source_seconds"] >= 2:
            output.append(
                {
                    "number": number,
                    "first": anchor(samples[0]),
                    "last": anchor(samples[-1]),
                    "evidence_ids": [o["evidence_id"] for o in samples],
                }
            )
    return output


def assign_reversed_blanks(samples, claims, counters):
    missing = set(range(1, max(claims, default=0) + 1)) - set(claims)
    if not missing or not counters or max(c["number"] for c in counters) <= max(missing):
        return
    groups = defaultdict(dict)
    for sample in samples:
        columns = tuple(
            sorted(
                {
                    p["column_hint"]
                    for p in sample.get("scoreboard", {}).get("unassigned_blank_plates", [])
                    if p.get("status") == "reversed_blank_plate_candidate"
                }
            )
        )
        if len(columns) == len(missing):
            groups[columns][sample["evidence_id"]] = sample
    supported = []
    for columns, evidence in groups.items():
        rows = sorted(evidence.values(), key=lambda o: o["source_seconds"])
        if len(rows) >= 3 and rows[-1]["source_seconds"] - rows[0]["source_seconds"] >= 2:
            supported.append((columns, rows))
    if len(supported) != 1:
        return
    columns, rows = supported[0]
    for number in sorted(missing):
        claims[number] = {
            "official_number": number,
            "color": "blank",
            "cumulative_points": 0,
            "identity_basis": "only_missing_ends_and_repeated_simultaneous_reversed_plates",
            "unassigned_column_hints": list(columns),
            "first": anchor(rows[0]),
            "last": anchor(rows[-1]),
            "evidence_ids": [o["evidence_id"] for o in rows],
            "observation_ids": [o["id"] for o in rows],
            "supporting_frames": len(rows),
            "competing_readings": [],
        }


def partition_scorecards(observations, partition):
    by_evidence = {o["evidence_id"]: o for o in observations}

    def clear_header(epoch):
        for identifier in epoch["evidence_ids"]:
            board = by_evidence[identifier]["scoreboard"]
            if board.get("blank_end_cards") or any(
                p.get("status") == "reversed_blank_plate_candidate"
                for p in board.get("unassigned_blank_plates", [])
            ):
                return False
            number = (board.get("current_end") or {}).get("number")
            if valid_integer(number, 2, 16):
                return False
        return True

    clears = [
        e
        for e in paired_epochs(observations)
        if not e["issues"] and not any(e["cards"].values()) and clear_header(e)
    ]
    # Every clear epoch is a possible separator. Idle subgroups produce no game.
    boundaries = sorted({e["first_seconds"] for e in clears})
    buckets = defaultdict(list)
    for observation in observations:
        buckets[bisect_right(boundaries, observation["source_seconds"])].append(observation)
    output = []
    for index, samples in sorted(buckets.items()):
        claims, conflicts = stable_claims(samples)
        if not claims and not conflicts:
            continue
        counters = current_end_evidence(samples)
        if not conflicts:
            assign_reversed_blanks(samples, claims, counters)
        ends = score_rows(claims)
        left = next((e for e in clears if index and e["first_seconds"] == boundaries[index - 1]), None)
        right = next(
            (e for e in clears if index < len(boundaries) and e["first_seconds"] == boundaries[index]), None
        )
        complete = bool(ends) and all(end["status"] == "proposed" for end in ends) and not conflicts
        document = {
            "source_id": partition[0],
            "source_metadata_sha256": partition[1],
            "reader_version": partition[2],
            "first_seconds": min(s["source_seconds"] for s in samples),
            "last_seconds": max(s["source_seconds"] for s in samples),
            "left_censored": left is None,
            "right_censored": right is None,
            "before_reset_epoch": left,
            "after_reset_epoch": right,
            "game_boundary_status": "board_reset_candidate" if right or left else "unresolved",
            "ends": ends,
            "card_conflicts": conflicts,
            "score_history_complete_through": len(ends) if complete else None,
            "known_end_scores": sum(end["status"] == "proposed" for end in ends),
            "highest_posted_end": max(claims, default=None),
            "current_end_evidence": counters,
            "highest_current_end": max((c["number"] for c in counters), default=None),
            "pending_end_numbers": sorted(
                set(range(1, max((c["number"] for c in counters), default=1))) - set(claims)
            ),
            "ends_played": None,
            "game_completion": "unknown",
            "final_result": None,
            "initial_totals": {"red": 0, "yellow": 0},
            "initial_totals_basis": "ordinary_game_cumulative_score_convention; handicap_not_configured",
            "acceptance": "proposed",
            "limitations": [
                "board_reset_may_be_correction",
                "last_posted_end_may_not_be_final_played_end",
                "play_times_require_ice_evidence",
            ],
        }
        output.append(identified(document))
    return output


def reconstruct_scorecards(source_id, observations, scope=None):
    partitions = defaultdict(list)
    by_time = defaultdict(list)
    for observation in observations:
        seconds = observation.get("source_seconds")
        manifest = observation_manifest(observation)
        if (
            observation.get("source_id") != source_id
            or not manifest
            or not observation.get("model_version")
            or not observation.get("id")
            or not observation.get("evidence_id")
            or not isinstance(seconds, (int, float))
            or not math.isfinite(seconds)
            or seconds < 0
            or (scope and not scope[0] <= seconds < scope[1])
        ):
            continue
        key = (source_id, manifest, observation["model_version"])
        by_time[(key, seconds)].append(observation)
    for (partition, _), alternatives in sorted(by_time.items()):
        # Duplicate observations/crops at one source time count once. Disagreement abstains.
        readings = {tuple(sorted(visible_claims(o))) for o in alternatives}
        if len(readings) != 1:
            continue
        partitions[partition].append(min(alternatives, key=lambda o: o["id"]))
    cards = [
        card
        for partition, samples in sorted(partitions.items())
        for card in partition_scorecards(samples, partition)
    ]
    document = {
        "version": VERSION,
        "source_id": source_id,
        "scope": list(scope) if scope else None,
        "scorecards": cards,
        "summary": {
            "candidate_games": len(cards),
            "known_end_scores": sum(c["known_end_scores"] for c in cards),
            "complete_posted_histories": sum(c["score_history_complete_through"] is not None for c in cards),
            "complete_games": 0,
        },
        "notice": "Repeated individual end-card sightings are reconciled across time "
        "within board reset boundaries. "
        "Complete posted scores do not establish that the final physical end was observed or posted. "
        "Blank ends require center-row plate evidence; missing scoring cards remain unknown.",
    }
    return {"sha256": digest(document), **document}
