"""Consolidate overlapping observations without counting camera views as extra shots."""

from uuid import NAMESPACE_URL, uuid5

from hogline.reconstruction.skill_actors import actor_key

VERSION = "overlapping-delivery-observations-v2"


def overlaps(left, right):
    return left["source_id"] == right["source_id"] and max(
        left["start_seconds"], right["start_seconds"]
    ) <= min(left["end_seconds"], right["end_seconds"])


def delivery_candidates(sequences, shots=()):
    events = []
    for sequence in sequences:
        if sequence.get("stale"):
            continue
        for index, delivery in enumerate(sequence["proposal"]["deliveries"]):
            samples = sequence["samples"]
            events.append(
                {
                    "sequence_id": sequence["id"],
                    "delivery_index": index,
                    "source_id": sequence["source_id"],
                    "created_at": sequence["created_at"],
                    "start_seconds": samples[delivery["first_frame"]]["source_seconds"],
                    "end_seconds": samples[delivery["last_frame"]]["source_seconds"],
                    "first_frame": delivery["first_frame"],
                    "last_frame": delivery["last_frame"],
                    "release_seconds": samples[delivery["release_frame"]]["source_seconds"]
                    if delivery["release_frame"] is not None
                    else None,
                    "stone_color": delivery["stone_color"],
                    "call": delivery["call"],
                    "description": delivery["thrower_description"],
                    "thrower_actor_id": actor_key(sequence["id"], delivery["thrower_actor_tag"])
                    if delivery.get("thrower_actor_tag")
                    else None,
                }
            )
    groups = []
    # Pairwise connected components: order of API/SQL rows must not affect the grouping.
    for event in sorted(events, key=lambda e: (e["source_id"], e["start_seconds"], e["sequence_id"])):
        matches = [
            group
            for group in groups
            if any(overlaps(other, event) and other["stone_color"] == event["stone_color"] for other in group)
        ]
        merged = [event]
        for group in matches:
            merged.extend(group)
            groups.remove(group)
        groups.append(merged)
    known = [group for group in groups if group[0]["stone_color"] != "unknown"]
    unresolved = []
    ambiguous = set()
    for group in (g for g in groups if g[0]["stone_color"] == "unknown"):
        matches = [
            other
            for other in known
            if any(b["stone_color"] != "unknown" and overlaps(a, b) for a in group for b in other)
        ]
        if len(matches) == 1:
            matches[0].extend(group)
        else:
            unresolved.append(group)
            if len(matches) > 1:
                ambiguous.update((e["sequence_id"], e["delivery_index"]) for e in group)
    groups = known + unresolved
    result = []
    for events in groups:
        events.sort(key=lambda e: (e["created_at"], e["sequence_id"], e["delivery_index"]))
        first = events[0]
        identifier = str(
            uuid5(NAMESPACE_URL, f"skill-delivery:{first['sequence_id']}:{first['delivery_index']}")
        )
        references = {
            f"skill-sequence:{event['sequence_id']}:frame:{index}"
            for event in events
            for index in range(event["first_frame"], event["last_frame"] + 1)
        }
        linked = [
            shot
            for shot in shots
            if getattr(shot, "acceptance", "proposed") != "excluded" and set(shot.evidence) & references
        ]
        excluded = [
            shot
            for shot in shots
            if getattr(shot, "acceptance", "proposed") == "excluded" and set(shot.evidence) & references
        ]
        known_calls = {event["call"] for event in events if event["call"] != "unknown"}
        known_colors = {event["stone_color"] for event in events if event["stone_color"] != "unknown"}
        result.append(
            {
                "id": identifier,
                "draft_id": linked[0].id if len(linked) == 1 else identifier,
                "source_id": first["source_id"],
                "stone_color": next(iter(known_colors)) if len(known_colors) == 1 else "unknown",
                "start_seconds": min(e["start_seconds"] for e in events),
                "end_seconds": max(e["end_seconds"] for e in events),
                "call": next(iter(known_calls)) if len(known_calls) == 1 else "unknown",
                "call_conflict": len(known_calls) > 1,
                "observations": events,
                "linked_shot_ids": [s.id for s in linked],
                "excluded_shot_ids": [s.id for s in excluded],
                "duplicate_review": len(linked) > 1,
                "ambiguous_overlap": any(
                    (e["sequence_id"], e["delivery_index"]) in ambiguous for e in events
                ),
                "method": VERSION,
                "acceptance": "proposed",
            }
        )
    return sorted(result, key=lambda g: (g["source_id"], g["start_seconds"], g["id"]))
