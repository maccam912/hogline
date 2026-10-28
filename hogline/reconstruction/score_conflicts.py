"""Compare saved scores with pinned board proposals without changing either."""

from hogline.adapters.sources import digest

VERSION = "saved-end-board-disagreement-v1"


def score_conflicts(data, broadcast):
    sources = {s["id"]: s for s in data["sources"]}
    evidence = {e["id"]: e for e in data["evidence"]}
    conflicts = []
    for game in data["games"]:
        if game.get("retired_into"):
            continue
        for end in game["ends"]:
            number = end.get("official_number")
            if number is None or end["state"] not in ("played", "blank"):
                continue
            proposals = []
            for p in broadcast.get("score_proposals", []):
                source = sources.get(p["source_id"])
                if (
                    p["official_number"] != number
                    or source is None
                    or p.get("source_metadata_sha256") != source["metadata_sha256"]
                    or p.get("model_version") != broadcast["version"]
                    or not p["evidence_ids"]
                    or not any(
                        span["source_id"] == p["source_id"]
                        and span["start_seconds"]
                        <= p["source_start_seconds"]
                        <= p["source_end_seconds"]
                        <= span["end_seconds"]
                        for span in game["spans"]
                    )
                ):
                    continue
                if any(
                    eid not in evidence
                    or evidence[eid]["source_id"] != p["source_id"]
                    or not p["source_start_seconds"]
                    <= evidence[eid]["source_seconds"]
                    <= p["source_end_seconds"]
                    for eid in p["evidence_ids"]
                ):
                    continue
                red = p["points"] if p["color"] == "red" else 0
                yellow = p["points"] if p["color"] == "yellow" else 0
                proposals.append(
                    {
                        "proposal_id": p["id"],
                        "source_id": p["source_id"],
                        "source_metadata_sha256": p["source_metadata_sha256"],
                        "model_version": p["model_version"],
                        "board_epoch_id": p["board_epoch_id"],
                        "red_points": red,
                        "yellow_points": yellow,
                        "source_start_seconds": p["source_start_seconds"],
                        "source_end_seconds": p["source_end_seconds"],
                        "evidence_ids": p["evidence_ids"],
                        "youtube_links": p["youtube_links"],
                        "agrees_with_saved": (red, yellow) == (end["red_points"], end["yellow_points"]),
                    }
                )
            if not any(not p["agrees_with_saved"] for p in proposals):
                continue
            document = {
                "version": VERSION,
                "game_id": game["id"],
                "game_revision": game["revision"],
                "game_label": game["label"],
                "sheet": game["sheet"],
                "end_id": end["id"],
                "official_number": number,
                "saved_score": {
                    k: end[k] for k in ("red_points", "yellow_points", "acceptance", "evidence_ids")
                },
                "proposals": sorted(proposals, key=lambda p: p["proposal_id"]),
                "notice": (
                    "Repeated board readings differ from a saved end. Possible causes include reader error, "
                    "missing cards, a wrong game span, or a score needing correction. "
                    "Neither interpretation is changed automatically."
                ),
            }
            document["id"] = digest(document)
            conflicts.append(document)
    return sorted(conflicts, key=lambda c: (c["sheet"], c["game_id"], c["official_number"], c["id"]))


def validate_review_export(section, data, broadcast):
    expected = {c["id"]: c for c in score_conflicts(data, broadcast)}
    actual = section["conflicts"]
    if (
        section["version"] != VERSION
        or len(actual) != len(expected)
        or {c["id"] for c in actual} != set(expected)
    ):
        raise ValueError("Release score disagreements differ from saved ends and board proposals")
    history = {}
    for review in section["reviews"]:
        document = {k: v for k, v in review.items() if k not in ("id", "sha256")}
        frozen = review["conflict"]
        if (
            digest(document) != review["sha256"]
            or digest({k: v for k, v in frozen.items() if k != "id"}) != frozen["id"]
        ):
            raise ValueError("Release score review audit checksum mismatch")
        history.setdefault(frozen["id"], []).append(review)
    for reviews in history.values():
        if sorted(r["review_revision"] for r in reviews) != list(range(1, len(reviews) + 1)):
            raise ValueError("Release score review audit revisions are incomplete")
    for conflict in actual:
        frozen = {
            k: v for k, v in conflict.items() if k not in ("review_revision", "review_state", "latest_review")
        }
        latest = max(history.get(conflict["id"], []), key=lambda r: r["review_revision"], default=None)
        review_state = "acknowledged" if latest and latest["decision"] == "keep_saved_score" else "open"
        if (
            frozen != expected[conflict["id"]]
            or conflict["latest_review"] != latest
            or conflict["review_state"] != review_state
            or conflict["review_revision"] != (latest["review_revision"] if latest else 0)
        ):
            raise ValueError("Release score disagreement review does not match its audit history")
    if section["open_count"] != sum(c["review_state"] == "open" for c in actual):
        raise ValueError("Release score disagreement count is inconsistent")
