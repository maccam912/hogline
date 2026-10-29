"""Combine independent board and reviewed final-ice claims without hiding disagreement."""

from collections import defaultdict

from hogline.adapters.sources import digest

VERSION = "board-and-reviewed-ice-scores-v2"
LEGACY_VERSION = "board-and-reviewed-ice-scores-v1"
COLORS = ("red", "yellow")


def reconcile_scores(report, reviews, *, version=VERSION):
    if version not in (VERSION, LEGACY_VERSION):
        raise ValueError("Unsupported combined score history version")
    source_id = report["source_id"]
    relevant = [r for r in reviews if r["source_id"] == source_id]
    superseded = {i for r in relevant for i in r["request"]["supersedes_review_ids"]}
    active = [r for r in relevant if r["id"] not in superseded]
    books = {h["id"]: h for h in report["scorecards"]}
    groups = defaultdict(list)
    for review in active:
        request = review["request"]
        key = request["board_history_id"] or f"ice:{request['game_key']}"
        if request["board_history_id"] and key not in books:
            continue
        groups[key].append(review)
    histories = []

    def first_time(key):
        if key in books:
            return books[key].get("first_seconds", 0)
        return min(pin["source_seconds"] for r in groups[key] for pin in r["evidence_refs"].values())

    for key in sorted(set(books) | set(groups), key=lambda key: (first_time(key), key)):
        book = books.get(key)
        board = {e["official_number"]: e for e in book["ends"]} if book else {}
        by_end = defaultdict(list)
        for r in groups[key]:
            by_end[r["request"]["official_end_number"] or r["request"]["observed_end_index"]].append(r)
        ends = []
        totals = dict.fromkeys(COLORS, 0)
        for number in range(1, max([*board, *by_end], default=0) + 1):
            original = board.get(number)
            board_score = (
                original["points"]
                if original and all(v is not None for v in original["points"].values())
                else None
            )
            observations = by_end[number]
            final_claims = [
                r
                for r in observations
                if r["request"].get("end_completed") is True
                and r["request"]["phase"] != "unresolved"
                and r["request"]["red_points"] is not None
            ]
            lying_claims = {
                (r["request"]["red_points"], r["request"]["yellow_points"])
                for r in observations
                if r["request"]["red_points"] is not None
            }
            claims = {(r["request"]["red_points"], r["request"]["yellow_points"]) for r in final_claims}
            ice_score = dict(zip(COLORS, next(iter(claims)), strict=True)) if len(claims) == 1 else None
            disagreement = len(claims) > 1 or bool(
                board_score is not None and ice_score is not None and board_score != ice_score
            )
            points = None if disagreement else board_score if board_score is not None else ice_score
            if points is None:
                totals = dict.fromkeys(COLORS, None)
            else:
                totals = {c: totals[c] + points[c] if totals[c] is not None else None for c in COLORS}
            ends.append(
                {
                    "official_number": number
                    if book or any(r["request"]["official_end_number"] for r in observations)
                    else None,
                    "observed_index": number,
                    "points": points or dict.fromkeys(COLORS, None),
                    "score_after_end": dict(totals),
                    "board_points": board_score,
                    "ice_points": ice_score,
                    "lying_ice_points": dict(zip(COLORS, next(iter(lying_claims)), strict=True))
                    if len(lying_claims) == 1
                    else None,
                    "end_completion_verified": any(
                        r["request"].get("end_completed") is True for r in observations
                    ),
                    "comparison": "disagreement"
                    if disagreement
                    else "agreement"
                    if board_score is not None and ice_score is not None
                    else "scoreboard_only"
                    if board_score is not None
                    else "ice_only"
                    if ice_score is not None
                    else "unresolved",
                    "status": "disputed"
                    if disagreement
                    else "proposed"
                    if points is not None
                    else "unresolved",
                    "board_evidence": original["board_evidence"] if original else None,
                    "ice_review_ids": sorted(r["id"] for r in observations),
                    "ice_evidence_ids": sorted(
                        {i for r in observations for i in r["request"]["final_state_evidence_ids"]}
                    ),
                    "phase_evidence_ids": sorted(
                        {i for r in observations for i in r["request"]["phase_evidence_ids"]}
                    ),
                    "state": "unknown"
                    if points is None
                    else "blank"
                    if not any(points.values())
                    else "played",
                }
            )
        completion_claims = [
            r
            for r in groups[key]
            if r["request"]["phase"] == "game_finished"
            and r["request"].get("end_completed") is True
            and (r["request"]["official_end_number"] or r["request"]["observed_end_index"]) == len(ends)
        ]
        complete = bool(ends and completion_claims and all(e["status"] == "proposed" for e in ends))
        prefix = 0
        for end in ends:
            if end["status"] != "proposed":
                break
            prefix += 1
        history = {
            "basis_key": key,
            "source_id": source_id,
            "board_history_id": book["id"] if book else None,
            "game_key": groups[key][0]["request"]["game_key"] if groups[key] else None,
            "first_seconds": first_time(key),
            "last_seconds": book.get("last_seconds", 0)
            if book
            else max(pin["source_seconds"] for r in groups[key] for pin in r["evidence_refs"].values()),
            "initial_totals_basis": "ordinary_game_cumulative_score_convention; handicap_not_configured",
            "ends": ends,
            "known_end_scores": sum(e["status"] == "proposed" for e in ends),
            "score_history_complete_through": (prefix or None)
            if version == VERSION
            else len(ends)
            if ends and all(e["status"] == "proposed" for e in ends)
            else None,
            "game_completion": "complete" if complete else "unknown",
            "ends_played": len(ends) if complete else None,
            "final_result": dict(totals) if complete else None,
            "completion_review_ids": sorted(r["id"] for r in completion_claims),
            "acceptance": "proposed",
            "coverage": "partial"
            if book and (book["left_censored"] or book["right_censored"])
            else "unassessed",
            "notice": "Independent visual reviews retain their author and evidence. "
            "Assistant reviews remain proposals; "
            "completed score histories do not establish first/final throw timing or full physical coverage.",
        }
        history["id"] = digest(history)
        histories.append(history)
    document = {
        "version": version,
        "source_id": source_id,
        "histories": histories,
        "unmatched_review_ids": sorted(
            r["id"]
            for r in active
            if r["request"]["board_history_id"] and r["request"]["board_history_id"] not in books
        ),
        "summary": {
            "known_end_scores": sum(h["known_end_scores"] for h in histories),
            "proposed_complete_games": sum(h["game_completion"] == "complete" for h in histories),
            "score_disagreements": sum(
                e["comparison"] == "disagreement" for h in histories for e in h["ends"]
            ),
        },
    }
    return {**document, "sha256": digest(document)}
