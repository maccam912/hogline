"""Game/end evidence ledgers. Posted cards, play and geometric scores stay separate."""

from collections import defaultdict
from uuid import NAMESPACE_URL, uuid5

from hogline.adapters.sources import digest
from hogline.reconstruction.board_events import board_timeline
from hogline.reconstruction.house_states import VERSION as HOUSE_VERSION
from hogline.reconstruction.play import VERSION as PLAY_VERSION
from hogline.reconstruction.play import bracket_play_changes

VERSION = "end-evidence-ledger-v1"


def identified(document):
    return {"id": str(uuid5(NAMESPACE_URL, digest(document))), **document}


def paired_epochs(observations):
    """Both complete rows must share three original evidence frames, not just nearby times."""
    timeline = board_timeline(observations)
    by_id = {o["evidence_id"]: o for o in observations}
    groups = defaultdict(lambda: defaultdict(list))
    for epoch in timeline["epochs"]:
        key = (epoch["source_id"], epoch["source_metadata_sha256"], epoch["model_version"])
        groups[key][epoch["color"]].append(epoch)
    pairs = []
    for partition, colors in sorted(groups.items()):
        for red in colors["red"]:
            for yellow in colors["yellow"]:
                common = set(red["evidence_ids"]) & set(yellow["evidence_ids"])
                frames = sorted((by_id[i] for i in common), key=lambda o: o["source_seconds"])
                if len(frames) < 3 or frames[-1]["source_seconds"] - frames[0]["source_seconds"] < 2:
                    continue
                cards = {"red": red["cards"], "yellow": yellow["cards"]}
                all_numbers = [c["end_number"] for row in cards.values() for c in row]
                issues = []
                if len(all_numbers) != len(set(all_numbers)):
                    issues.append("same_end_on_both_color_rows")
                for row in cards.values():
                    points = [c["cumulative_points"] for c in sorted(row, key=lambda c: c["end_number"])]
                    if any(not 1 <= b - a <= 8 for a, b in zip(points, points[1:], strict=False)):
                        issues.append("invalid_cumulative_progression")
                pairs.append(
                    identified(
                        {
                            "source_id": partition[0],
                            "source_metadata_sha256": partition[1],
                            "reader_version": partition[2],
                            "cards": cards,
                            "first_seconds": frames[0]["source_seconds"],
                            "last_seconds": frames[-1]["source_seconds"],
                            "first": {
                                k: frames[0].get(k)
                                for k in ("id", "evidence_id", "source_seconds", "youtube_url")
                            },
                            "evidence_ids": [f["evidence_id"] for f in frames],
                            "row_epoch_ids": [red["id"], yellow["id"]],
                            "issues": sorted(set(issues)),
                        }
                    )
                )
    return sorted(
        pairs,
        key=lambda p: (
            p["source_id"],
            p["source_metadata_sha256"],
            p["reader_version"],
            p["first_seconds"],
            p["id"],
        ),
    )


def ledger(epochs, boundary_reason):
    latest = epochs[-1]
    cards = {
        color: {c["end_number"]: c["cumulative_points"] for c in latest["cards"][color]}
        for color in ("red", "yellow")
    }
    highest = max([n for row in cards.values() for n in row], default=0)
    ends = []
    for number in range(1, highest + 1):
        owners = [color for color in cards if number in cards[color]]
        color = owners[0] if len(owners) == 1 else None
        points = {"red": None, "yellow": None}
        posting = None
        if color:
            predecessors = [n for n in cards[color] if n < number]
            if predecessors:
                points[color] = cards[color][number] - cards[color][max(predecessors)]
                points["yellow" if color == "red" else "red"] = 0
            first_index = next(
                i for i, e in enumerate(epochs) if any(c["end_number"] == number for c in e["cards"][color])
            )
            first = epochs[first_index]
            before = epochs[first_index - 1] if first_index else None
            posting = {
                "earliest_seconds": before["last_seconds"] if before else None,
                "latest_seconds": first["first_seconds"],
                "left_censored": before is None,
                "first_observation": first["first"],
                "epoch_id": first["id"],
                "evidence_ids": first["evidence_ids"],
                "meaning": "card_posting_or_readability_change; not play completion",
            }
        cumulative = {}
        for team, row in cards.items():
            preceding = [n for n in row if n <= number]
            cumulative[team] = row[max(preceding)] if preceding and color else None
        ends.append(
            {
                "official_number": number,
                "scoring_color": color,
                "points": points,
                "score_after_end": cumulative,
                "board_posting": posting,
                "status": "proposed" if color else "unknown",
                "unknown_reasons": (
                    ["initial_cumulative_baseline_unobserved"] if color and points[color] is None else []
                )
                if color
                else ["no_scoring_card; blank_or_missing_end_unresolved"],
                "first_delivery": None,
                "last_stone_stopped": None,
                "score_decided": None,
            }
        )
    return identified(
        {
            "boundary_reason": boundary_reason,
            "source_metadata_sha256": latest["source_metadata_sha256"],
            "reader_version": latest["reader_version"],
            "first_seconds": epochs[0]["first_seconds"],
            "last_seconds": latest["last_seconds"],
            "board_epoch_ids": [e["id"] for e in epochs],
            "ends": ends,
            "highest_observed_end_number": highest or None,
            "ends_played": None,
            "scheduled_ends": None,
            "game_completion": "unknown",
            "acceptance": "proposed",
            "final_result": None,
        }
    )


def board_ledgers(epochs):
    output, current, previous_partition = [], [], None
    reason = "recording_or_input_boundary"
    for epoch in epochs:
        partition = (epoch["source_id"], epoch["source_metadata_sha256"], epoch["reader_version"])
        if epoch["issues"]:
            if current:
                output.append(ledger(current, reason))
            current, reason, previous_partition = [], "conflicting_board_reading", None
            continue
        if current:
            previous = current[-1]
            removed_or_moved = any(
                not {(c["end_number"], c["cumulative_points"]) for c in previous["cards"][color]}
                <= {(c["end_number"], c["cumulative_points"]) for c in epoch["cards"][color]}
                for color in ("red", "yellow")
            )
            if partition != previous_partition or removed_or_moved:
                output.append(ledger(current, reason))
                reason = "input_boundary" if partition != previous_partition else "board_reset_or_correction"
                current = []
        current.append(epoch)
        previous_partition = partition
    if current:
        output.append(ledger(current, reason))
    return output


def physical_candidates(house_results, house_scores):
    """A pre-clearing stable observation is useful; it is not proof of the final shot."""
    candidates, seen = [], set()
    for result in [*house_results, *bracket_play_changes(house_results)]:
        if result.get("version") not in (HOUSE_VERSION, PLAY_VERSION) or result.get("status") != "derived":
            continue
        states = {s["id"]: s for s in result["states"]}
        for change in result["transitions"]:
            if change["kind"] != "possible_clearing":
                continue
            before = states[change["before_state_id"]]
            key = (
                result["source_metadata_sha256"],
                result["profile_sha256"],
                change["view"],
                change["before"]["decoded_rgb_sha256"],
                change["after"]["decoded_rgb_sha256"],
            )
            if key in seen:
                continue
            seen.add(key)
            # A geometric score must refer to an exact retained state endpoint, same view/profile.
            scores = [
                s
                for s in house_scores
                if s["source_id"] == result["source_id"]
                and s["source_metadata_sha256"] == result["source_metadata_sha256"]
                and s["profile_sha256"] == result["profile_sha256"]
                and s["decoded_rgb_sha256"]
                in (before["first"]["decoded_rgb_sha256"], before["last"]["decoded_rgb_sha256"])
            ]
            score_refs = [
                {"observation_id": s["id"], **s["views"][change["view"]]["scoring"]} for s in scores
            ]
            candidates.append(
                identified(
                    {
                        "window_id": result["window_id"],
                        "house_result_id": result["id"],
                        "house_result_sha256": result["sha256"],
                        "supporting_window_ids": result.get("supporting_window_ids", [result["window_id"]]),
                        "view": change["view"],
                        "source_metadata_sha256": result["source_metadata_sha256"],
                        "profile_sha256": result["profile_sha256"],
                        "settled_house_observed": {"first": before["first"], "last": before["last"]},
                        "clearing": change,
                        "house_score_proposals": score_refs,
                        "first_delivery": None,
                        "last_stone_stopped": None,
                        "score_decided": None,
                        "official_end_number": None,
                        "board_comparisons": [],
                        "acceptance": "proposed",
                        "unknown_reasons": [
                            "clearing_may_be_practice_or_cleanup",
                            "last_observed_stable_state_may_miss_final_stones",
                            "first_and_last_physical_deliveries_unverified",
                        ],
                    }
                )
            )
    return sorted(candidates, key=lambda c: (c["clearing"]["earliest_seconds"], c["view"], c["id"]))


def reconstruct_ends(source_id, observations, house_results=(), house_scores=(), scope=None):
    observations = [o for o in observations if o["source_id"] == source_id]
    house_results = [r for r in house_results if r["source_id"] == source_id]
    house_scores = [r for r in house_scores if r["source_id"] == source_id]
    if scope:
        start, end = scope
        observations = [o for o in observations if start <= o["source_seconds"] < end]
        # Whole state/result must fit. Clipping could manufacture a cleanup or final state.
        house_results = [
            r
            for r in house_results
            if all(start <= s["start_seconds"] <= s["end_seconds"] < end for s in r["states"])
        ]
    epochs = paired_epochs(observations)
    ledgers = board_ledgers(epochs)
    physical = physical_candidates(house_results, house_scores)
    for candidate in physical:
        stable = candidate["settled_house_observed"]["first"]["source_seconds"]
        clear = candidate["clearing"]["latest_seconds"]
        matches = []
        for book in ledgers:
            if book["source_metadata_sha256"] != candidate["source_metadata_sha256"]:
                continue
            for end in book["ends"]:
                posting = end["board_posting"]
                if (
                    posting
                    and not posting["left_censored"]
                    and stable <= posting["latest_seconds"] <= clear + 180
                    and posting["earliest_seconds"] >= stable - 180
                ):
                    matches.append((book, end))
        for book, end in matches:
            competing = [
                p
                for p in physical
                if p["id"] != candidate["id"]
                and p["source_metadata_sha256"] == candidate["source_metadata_sha256"]
                and p["settled_house_observed"]["first"]["source_seconds"]
                <= end["board_posting"]["latest_seconds"]
                <= p["clearing"]["latest_seconds"] + 180
            ]
            unique = len(matches) == 1 and not competing
            proposals = [
                p["provisional_score"]
                for p in candidate["house_score_proposals"]
                if p.get("provisional_score") is not None
            ]
            score = proposals[0] if proposals and all(p == proposals[0] for p in proposals) else None
            known = all(n is not None for n in end["points"].values())
            candidate["board_comparisons"].append(
                {
                    "ledger_id": book["id"],
                    "official_number": end["official_number"],
                    "association": "proposed_unique" if unique else "ambiguous",
                    "board_points": end["points"],
                    "vision_points": score,
                    "comparison": ("agrees" if score == end["points"] else "disagrees")
                    if unique and known and score
                    else "unresolved",
                    "board_posting": end["board_posting"],
                    "posting_lag_seconds": None,
                    "unknown_reason": "play_completion_time_unverified; no measured_posting_lag",
                }
            )
        if (
            len(candidate["board_comparisons"]) == 1
            and candidate["board_comparisons"][0]["association"] == "proposed_unique"
        ):
            candidate["official_end_number"] = candidate["board_comparisons"][0]["official_number"]
    document = {
        "schema_version": "hogline-end-timeline-v1",
        "version": VERSION,
        "source_id": source_id,
        "scope": list(scope) if scope else None,
        "paired_board_epochs": epochs,
        "board_ledgers": ledgers,
        "end_candidates": physical,
        "arrival_candidates": list(
            {
                (a["view"], a["before"]["decoded_rgb_sha256"], a["after"]["decoded_rgb_sha256"]): a
                for r in [*house_results, *bracket_play_changes(house_results)]
                for a in r.get("arrival_candidates", [])
            }.values()
        ),
        "settling_candidates": [a for r in house_results for a in r.get("settling_candidates", [])],
        "coverage": {
            "board_observation_frames": len({o["evidence_id"] for o in observations}),
            "paired_row_epochs": len(epochs),
            "house_result_count": len(house_results),
        },
        "input_refs": {
            "broadcast": sorted((o["id"], digest(o)) for o in observations),
            "house_results": sorted((r["id"], r["sha256"]) for r in house_results),
            "house_scores": sorted((r["id"], digest(r)) for r in house_scores),
        },
        "notice": (
            "Board ledgers are candidates, not confirmed games. Resets/corrections split histories. "
            "Missing cards do not establish blanks or initial zero totals. Highest observed end is "
            "not the number played or scheduled. Pre-clearing stability is not proof of the final "
            "stone stopping or score agreement. Both camera views remain separate. "
            "Posting times never substitute for play times; all comparisons require review."
        ),
    }
    return {"sha256": digest(document), **document}
