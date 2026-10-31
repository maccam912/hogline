"""Anonymous, clip-scoped activity/role proposals; no named or cross-clip identity inference."""


def actor_key(sequence_id, actor_tag):
    return f"skill-actor:{sequence_id}:{actor_tag}"


def actor_proposals(sequences):
    actors = []
    for sequence in sequences:
        if sequence.get("stale"):
            continue
        grouped = {}
        for index, activity in enumerate(sequence["proposal"].get("activities", [])):
            grouped.setdefault(activity["actor_tag"], []).append({**activity, "activity_index": index})
        for tag, activities in grouped.items():
            sides = {a["team_side"] for a in activities if a["team_side"] != "unknown"}
            actions = {a["action"] for a in activities}
            roles = [
                role
                for action, role in [
                    ("delivery", "thrower"),
                    ("sweeping", "sweeper"),
                    ("calling_broom", "calling_broom_holder"),
                ]
                if action in actions
            ]
            frames = {a["key_frame"] for a in activities}
            first = min(sequence["samples"][a["first_frame"]]["source_seconds"] for a in activities)
            actors.append(
                {
                    "id": actor_key(sequence["id"], tag),
                    "sequence_id": sequence["id"],
                    "actor_tag": tag,
                    "source_id": sequence["source_id"],
                    "start_seconds": first,
                    "end_seconds": max(
                        sequence["samples"][a["last_frame"]]["source_seconds"] for a in activities
                    ),
                    "descriptions": list(dict.fromkeys(a["description"] for a in activities)),
                    "team_side": next(iter(sides)) if len(sides) == 1 else "unknown",
                    "side_conflict": len(sides) > 1,
                    "role_proposals": roles,
                    "throwing_position": None,
                    "skip": None,
                    "activities": activities,
                    "acceptance": "proposed",
                    "scope": "this_sequence_only",
                    "evidence": [f"skill-sequence:{sequence['id']}:frame:{i}" for i in sorted(frames)],
                    "notice": (
                        "Activity and team proposals require review. "
                        "Acting house player is not necessarily skip."
                    ),
                }
            )
    return sorted(actors, key=lambda a: (a["source_id"], a["start_seconds"], a["id"]))
