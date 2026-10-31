"""Order possibilities under reviewed end bounds, preserving unobserved deliveries."""

from hogline.adapters.sources import digest
from hogline.domain.skills import resolve_participant

VERSION = "reviewed-end-subsequence-order-v1"
POSITIONS = ("lead", "second", "third", "fourth")


def order_possibilities(events, first_side="unknown"):
    """All monotone subsequences of a standard 16-delivery end, using forward/backward DP.

    A missing observation can occupy ANY unused delivery index. We never assume the
    first observed delivery is first in the end, or that alternating colors prove
    continuous coverage. A confirmed team ordinal is a hard anchor.
    """
    possibilities = [set() for _ in events]
    for first in ["red", "yellow"] if first_side == "unknown" else [first_side]:
        other = "yellow" if first == "red" else "red"
        choices = []
        for event in events:
            allowed = set()
            for overall in range(1, 17):
                side = first if overall % 2 else other
                team_number = (overall + 1) // 2
                if event["side"] not in ("unknown", side):
                    continue
                if event.get("team_delivery") is not None and event["team_delivery"] != team_number:
                    continue
                allowed.add(overall)
            choices.append(allowed)
        forward, prior = [], {0}
        for allowed in choices:
            reachable = {n for n in allowed if any(p < n for p in prior)}
            forward.append(reachable)
            prior = reachable
        backward, following = [], {17}
        for allowed in reversed(choices):
            reachable = {n for n in allowed if any(p > n for p in following)}
            backward.append(reachable)
            following = reachable
        backward.reverse()
        for i, (left, right) in enumerate(zip(forward, backward, strict=True)):
            for overall in left & right:
                possibilities[i].add((first if overall % 2 else other, (overall + 1) // 2, overall))
    return [
        [
            {
                "side": side,
                "team_delivery": number,
                "overall_delivery": overall,
                "position": POSITIONS[(number - 1) // 2],
            }
            for side, number, overall in sorted(values)
        ]
        for values in possibilities
    ]


def candidate_events(candidates, document):
    shots = {s.id: s for s in document.shots}
    events, linked = [], set()
    for candidate in candidates:
        linked.update(candidate["linked_shot_ids"])
        linked.update(candidate.get("excluded_shot_ids", []))
        if not candidate["linked_shot_ids"] and candidate.get("excluded_shot_ids"):
            continue
        reviewed = [shots[s] for s in candidate["linked_shot_ids"] if s in shots]
        confirmed = [s for s in reviewed if s.acceptance == "confirmed"]
        anchor = confirmed[0] if len(confirmed) == 1 else None
        events.append(
            {
                "id": candidate["id"],
                "candidate_id": candidate["id"],
                "source_id": candidate["source_id"],
                "start_seconds": candidate["start_seconds"],
                "end_seconds": candidate["end_seconds"],
                "side": anchor.side if anchor else candidate["stone_color"],
                "team_delivery": anchor.team_delivery if anchor else None,
                "reviewed_end": anchor.end if anchor else None,
                "participant": resolve_participant(document, anchor.participant) if anchor else None,
                "confirmed_shot_id": anchor.id if anchor else None,
                "ambiguous": candidate["ambiguous_overlap"] or candidate["duplicate_review"],
            }
        )
    for shot in document.shots:
        if shot.id in linked or shot.acceptance != "confirmed":
            continue
        events.append(
            {
                "id": shot.id,
                "candidate_id": None,
                "source_id": shot.source_id,
                "start_seconds": shot.seconds,
                "end_seconds": shot.seconds,
                "side": shot.side,
                "team_delivery": shot.team_delivery,
                "reviewed_end": shot.end,
                "participant": resolve_participant(document, shot.participant),
                "confirmed_shot_id": shot.id,
                "ambiguous": False,
            }
        )
    return sorted(events, key=lambda e: (e["source_id"], e["start_seconds"], e["id"]))


def order_report(candidates, document, stale=False):
    events = candidate_events(candidates, document)
    windows, covered = [], set()
    for window in document.end_windows:
        if window.acceptance == "excluded":
            continue
        selected = [
            e
            for e in events
            if e["source_id"] == window.source_id
            and window.start_seconds <= e["start_seconds"] < window.end_seconds
        ]
        issues = []
        if stale:
            issues.append("stale_skill_review")
        if window.acceptance != "confirmed":
            issues.append("unconfirmed_end_interval")
        if any(
            e["source_id"] == window.source_id
            and e["start_seconds"] < window.start_seconds <= e["end_seconds"]
            for e in events
        ):
            issues.append("delivery_crosses_start_boundary")
        if any(e["end_seconds"] >= window.end_seconds for e in selected):
            issues.append("delivery_crosses_end_boundary")
        if any(e["reviewed_end"] not in (None, window.end) for e in selected):
            issues.append("confirmed_shot_end_conflicts_with_interval")
        if any(e["ambiguous"] for e in selected):
            issues.append("unresolved_delivery_overlap")
        if any(a["end_seconds"] >= b["start_seconds"] for a, b in zip(selected, selected[1:], strict=False)):
            issues.append("overlapping_delivery_intervals")
        values = order_possibilities(selected, window.first_side) if not issues else [[] for _ in selected]
        if selected and not issues and not all(values):
            issues.append("no_consistent_standard_order")
        entries = []
        for event, options in zip(selected, values, strict=True):
            sides = sorted({v["side"] for v in options})
            numbers = sorted({v["team_delivery"] for v in options})
            positions = [p for p in POSITIONS if any(v["position"] == p for v in options)]
            determined = len(sides) == len(numbers) == 1
            entries.append(
                {
                    **event,
                    "end": window.end,
                    "possibilities": options,
                    "side_options": sides,
                    "team_delivery_options": numbers,
                    "position_options": positions,
                    "proposed_side": sides[0] if len(sides) == 1 else None,
                    "proposed_team_delivery": numbers[0] if determined else None,
                    "proposed_position": positions[0] if len(sides) == len(positions) == 1 else None,
                    "acceptance": "proposed",
                    "skip": None,
                }
            )
            covered.add(event["id"])
        windows.append(
            {
                "window_id": window.id,
                "end": window.end,
                "source_id": window.source_id,
                "issues": issues,
                "entries": entries,
                "acceptance": "proposed",
            }
        )
    # These are search leads, not proof that a delivery is missing (end change,
    # color error, practice or duplicate detections can also explain repetition).
    leads = []
    for a, b in zip(events, events[1:], strict=False):
        if (
            a["source_id"] == b["source_id"]
            and a["side"] == b["side"] != "unknown"
            and a["end_seconds"] < b["start_seconds"]
        ):
            leads.append(
                {
                    "id": digest({"source": a["source_id"], "before": a["id"], "after": b["id"]}),
                    "source_id": a["source_id"],
                    "before_id": a["id"],
                    "after_id": b["id"],
                    "start_seconds": a["end_seconds"],
                    "end_seconds": b["start_seconds"],
                    "kind": "same_color_observations",
                    "side": a["side"],
                    "notice": "Inspect for an unobserved opposing delivery, wrong color, "
                    "duplicate or end change.",
                }
            )
    return {
        "version": VERSION,
        "windows": windows,
        "search_leads": leads,
        "unscoped_event_count": sum(e["id"] not in covered for e in events),
        "notice": "Standard four-player order only. Unobserved throws remain possible. "
        "All positions are proposals; fourth does not establish skip. "
        "Confirmed shot numbers constrain order; proposed numbers do not.",
    }
