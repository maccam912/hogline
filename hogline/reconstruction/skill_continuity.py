"""Reviewable anonymous continuity from shared events and identical-frame geometry."""

from collections import defaultdict
from itertools import combinations
from uuid import NAMESPACE_URL, uuid5

from hogline.domain.skills import participant_aliases
from hogline.reconstruction.skill_actors import actor_proposals

VERSION = "anonymous-shared-evidence-v1"


def intersection_over_union(a, b):
    def area(box):
        return max(0, box[2] - box[0]) * max(0, box[3] - box[1])

    intersection = area([max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])])
    union = area(a) + area(b) - intersection
    return intersection / union if union else 0


def source_box(activity, sample):
    if "crop_bounds_px" in sample:
        x1, y1, x2, y2 = [
            v / (sample["width"] if n % 2 == 0 else sample["height"])
            for n, v in enumerate(sample["crop_bounds_px"])
        ]
    else:
        x1, y1, x2, y2 = sample.get("normalized_crop_bounds", [0, 0, 1, 1])
    a, b, c, d = activity["bbox"]
    return [x1 + a * (x2 - x1), y1 + b * (y2 - y1), x1 + c * (x2 - x1), y1 + d * (y2 - y1)]


def continuity_proposals(sequences, candidates, document):
    actors = {a["id"]: a for a in actor_proposals(sequences)}
    aliases = participant_aliases(document)
    edges = defaultdict(list)

    def add(left, right, evidence):
        if left == right or left not in actors or right not in actors:
            return
        if actors[left]["sequence_id"] == actors[right]["sequence_id"]:
            return
        if aliases.get(left) and aliases.get(left) == aliases.get(right):
            return
        edges[tuple(sorted((left, right)))].append(evidence)

    for candidate in candidates:
        if candidate["ambiguous_overlap"] or candidate["duplicate_review"]:
            continue
        throwers = sorted({o["thrower_actor_id"] for o in candidate["observations"] if o["thrower_actor_id"]})
        for left, right in combinations(throwers, 2):
            add(
                left,
                right,
                {
                    "kind": "same_delivery_proposal",
                    "candidate_id": candidate["id"],
                    "source_id": candidate["source_id"],
                    "start_seconds": candidate["start_seconds"],
                    "end_seconds": candidate["end_seconds"],
                },
            )

    frames = defaultdict(list)
    by_sequence = {s["id"]: s for s in sequences}
    for actor in actors.values():
        sequence = by_sequence[actor["sequence_id"]]
        for activity in actor["activities"]:
            sample = sequence["samples"][activity["key_frame"]]
            if not sample.get("decoded_rgb_sha256"):
                continue
            key = (actor["source_id"], sample["decoded_rgb_sha256"], sample["source_seconds"])
            frames[key].append(
                {
                    "id": actor["id"],
                    "sequence_id": sequence["id"],
                    "box": source_box(activity, sample),
                    "frame": activity["key_frame"],
                }
            )
    for (source, image_hash, seconds), observations in frames.items():
        by_pair = {}
        for left, right in combinations(observations, 2):
            if left["sequence_id"] == right["sequence_id"]:
                continue
            overlap = intersection_over_union(left["box"], right["box"])
            if overlap < 0.55:
                continue
            pair = tuple(sorted((left["id"], right["id"])))
            if pair not in by_pair or overlap > by_pair[pair][0]:
                by_pair[pair] = (overlap, left, right)
        for pair, (overlap, left, right) in by_pair.items():
            # Competing boxes for another person in either sequence make this ambiguous.
            alternatives = [
                value[0]
                for other, value in by_pair.items()
                if other != pair
                and set(pair) & set(other)
                and {value[1]["sequence_id"], value[2]["sequence_id"]}
                == {left["sequence_id"], right["sequence_id"]}
            ]
            if alternatives and overlap - max(alternatives) < 0.15:
                continue
            add(
                *pair,
                {
                    "kind": "same_frame_body_overlap",
                    "source_id": source,
                    "source_seconds": seconds,
                    "decoded_rgb_sha256": image_hash,
                    "box_iou": overlap,
                    "observations": [left, right],
                },
            )
    proposals = []
    for pair, evidence in sorted(edges.items()):
        sides = {actors[a]["team_side"] for a in pair} - {"unknown"}
        assigned = {aliases[a] for a in pair if a in aliases}
        conflicts = []
        if len(sides) > 1:
            conflicts.append("conflicting_team_side_proposals")
        if len(assigned) > 1:
            conflicts.append("already_assigned_to_different_confirmed_participants")
        proposals.append(
            {
                "id": str(uuid5(NAMESPACE_URL, "skill-continuity:" + ":".join(pair))),
                "actor_ids": list(pair),
                "evidence": evidence,
                "conflicts": conflicts,
                "acceptance": "proposed",
                "method": VERSION,
                "notice": (
                    "Shared observations suggest continuity; review before combining participants. "
                    "No face or name matching."
                ),
            }
        )
    return proposals
