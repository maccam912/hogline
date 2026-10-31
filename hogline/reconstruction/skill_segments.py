"""Game/end/delivery hierarchy with explicit unknown action-completion boundaries."""

from hogline.adapters.sources import digest
from hogline.domain.skills import resolve_participant
from hogline.reconstruction.skill_order import POSITIONS, order_report

VERSION = "direction-end-delivery-hierarchy-v1"


def origin_hint(candidate, sequences, layouts):
    """Seed-layout far-house delivery cue, not whichever feed contains a moving stone."""
    evidence, views = [], set()
    if candidate["ambiguous_overlap"] or candidate["duplicate_review"]:
        return {"view": None, "status": "ambiguous_delivery", "evidence": []}
    for observation in candidate["observations"]:
        sequence = sequences[observation["sequence_id"]]
        layout = layouts.get(sequence["id"])
        if not layout or layout.get("id") != "wausau-composite-seed-v1":
            continue
        # Later sightings of the same stone are not a new origin observation.
        if observation["start_seconds"] > candidate["start_seconds"] + 2:
            continue
        for activity in sequence["proposal"].get("activities", []):
            if (
                activity["action"] != "delivery"
                or activity.get("delivery_index") != observation["delivery_index"]
                or activity["actor_tag"] != (observation["thrower_actor_id"] or "").split(":")[-1]
            ):
                continue
            view = sequence["request"].get("view", "composite")
            x1, y1, x2, y2 = activity["bbox"]
            if view == "composite":
                matches = [
                    (name, box)
                    for name, box in layout["views"].items()
                    if name.startswith("perspective_")
                    and box[0] <= x1 < x2 <= box[2]
                    and box[1] <= y1 < y2 <= box[3]
                ]
                if len(matches) != 1:
                    continue
                view, (left, top, right, bottom) = matches[0]
                x1, x2 = (x1 - left) / (right - left), (x2 - left) / (right - left)
                y1, y2 = (y1 - top) / (bottom - top), (y2 - top) / (bottom - top)
            if view not in ("perspective_left", "perspective_right"):
                continue
            # The controlled-delivery participant must be at the far house in
            # this seed layout. A near-camera continuation is not an origin cue.
            if not (0.18 <= (x1 + x2) / 2 <= 0.82 and 0.42 <= y2 <= 0.72 and 0.06 <= y2 - y1 <= 0.45):
                continue
            views.add(view)
            evidence.append(
                {
                    "sequence_id": sequence["id"],
                    "activity_tag": activity["actor_tag"],
                    "frame": activity["key_frame"],
                    "view": view,
                    "basis": "Proposed controlled delivery in the seed camera's far-house region",
                }
            )
    return {
        "view": next(iter(views)) if len(views) == 1 else None,
        "status": "origin_proposal"
        if len(views) == 1
        else "conflicting_origins"
        if views
        else "origin_unknown",
        "evidence": evidence,
        "acceptance": "proposed",
    }


def segment_hierarchy(game, document, candidates, sequences, layouts, rule, stale=False):
    index = {s["id"]: s for s in sequences if not s.get("stale")}
    order = order_report(candidates, document, stale)
    order_entries = {
        e["candidate_id"]: (w, e)
        for w in order["windows"]
        if not w["issues"]
        for e in w["entries"]
        if e.get("candidate_id")
    }
    shots = {s.id: s for s in document.shots}
    deliveries = []
    for candidate in candidates:
        if candidate["excluded_shot_ids"] and not candidate["linked_shot_ids"]:
            continue
        origin = (
            origin_hint(candidate, index, layouts)
            if not stale
            else {"view": None, "status": "stale_review", "evidence": []}
        )
        view = origin["view"]
        parity = None
        if view and rule.get("odd_delivery_view"):
            parity = "odd" if view == rule["odd_delivery_view"] else "even"
        accepted = [shots[s] for s in candidate["linked_shot_ids"] if shots[s].acceptance == "confirmed"]
        anchor = accepted[0] if len(accepted) == 1 and not stale else None
        entry = order_entries.get(candidate["id"])
        end = anchor.end if anchor else entry[1]["end"] if entry else None
        ordinal = anchor.team_delivery if anchor else entry[1]["proposed_team_delivery"] if entry else None
        side = anchor.side if anchor else candidate["stone_color"]
        issues = []
        if end is not None and parity and parity != ("odd" if end % 2 else "even"):
            issues.append("direction_parity_conflicts_with_reviewed_end")
        if candidate["ambiguous_overlap"] or candidate["duplicate_review"]:
            issues.append("ambiguous_delivery")
        deliveries.append(
            {
                "id": candidate["id"],
                "source_id": candidate["source_id"],
                "start_seconds": candidate["start_seconds"],
                "last_delivery_observed_seconds": candidate["end_seconds"],
                "release_seconds_options": sorted(
                    {
                        o["release_seconds"]
                        for o in candidate["observations"]
                        if o["release_seconds"] is not None
                    }
                ),
                "stone_end_seconds": None,
                "action_end_seconds": None,
                "completion_status": "not_observed",
                "side": side,
                "end": end,
                "team_delivery": ordinal,
                "position": POSITIONS[(ordinal - 1) // 2] if ordinal else None,
                "stone_in_position": (ordinal - 1) % 2 + 1 if ordinal else None,
                "participant": resolve_participant(document, anchor.participant) if anchor else None,
                "label_status": "reviewed"
                if anchor and ordinal
                else "order_proposal"
                if ordinal
                else "incomplete",
                "origin": origin,
                "end_parity": parity,
                "issues": issues,
                "acceptance": "proposed",
            }
        )
    groups = []
    for d in deliveries:
        # Reviewed numbered ends keep their explicit scope. Unnumbered runs are
        # only end-block proposals; direction cannot distinguish ends 1/3/5.
        span_index = next(
            (
                i
                for i, span in enumerate(game["spans"])
                if span["source_id"] == d["source_id"]
                and span["start_seconds"] <= d["start_seconds"] < span["end_seconds"]
            ),
            None,
        )
        key = (
            (d["source_id"], "numbered", d["end"])
            if d["end"]
            else (d["source_id"], "direction", d["origin"]["view"], span_index)
        )
        previous = groups[-1] if groups else None
        join = bool(
            previous
            and previous["group_key"] == key
            and (
                d["end"] is not None
                or (
                    d["origin"]["view"]
                    and d["start_seconds"] - previous["last_delivery_observed_seconds"] <= 300
                    and not d["issues"]
                    and not previous["issues"]
                )
            )
        )
        if not join:
            boundary = None
            if previous and previous["source_id"] == d["source_id"]:
                boundary = {
                    "earliest_seconds": previous["last_delivery_observed_seconds"],
                    "latest_seconds": d["start_seconds"],
                    "reason": "delivery_origin_changed"
                    if previous["delivery_view"]
                    and d["origin"]["view"]
                    and previous["delivery_view"] != d["origin"]["view"]
                    else "continuity_unresolved",
                    "acceptance": "proposed",
                }
            groups.append(
                {
                    "id": digest({"source": d["source_id"], "first": d["id"]})[:24],
                    "group_key": key,
                    "source_id": d["source_id"],
                    "end": d["end"],
                    "kind": "numbered_end" if d["end"] else "proposed_end_block",
                    "delivery_view": d["origin"]["view"],
                    "parity": d["end_parity"],
                    "observed_start_seconds": d["start_seconds"],
                    "last_delivery_observed_seconds": d["last_delivery_observed_seconds"],
                    "preceding_boundary": boundary,
                    "issues": list(d["issues"]),
                    "deliveries": [],
                }
            )
        groups[-1]["deliveries"].append(d)
        groups[-1]["issues"] = sorted(set(groups[-1]["issues"]) | set(d["issues"]))
        groups[-1]["last_delivery_observed_seconds"] = max(
            groups[-1]["last_delivery_observed_seconds"], d["last_delivery_observed_seconds"]
        )
    for group in groups:
        group.pop("group_key")
    return {
        "version": VERSION,
        "game": {
            "id": game["id"],
            "label": game.get("label"),
            "coverage": game.get("coverage", "unknown"),
            "acceptance": game.get("acceptance", "proposed"),
            "spans": game["spans"],
        },
        "direction_rule": rule,
        "end_groups": groups,
        "delivery_count": len(deliveries),
        "completed_action_count": 0,
        "stale": stale,
        "notice": (
            "Direction blocks are proposed ends, not complete coverage. "
            "Exact end/throw numbers need anchors. "
            "Last delivery sighting is not stone stop or action completion."
        ),
    }
