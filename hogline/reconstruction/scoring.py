"""Combine independent board and reviewed final-ice claims without hiding disagreement."""

from collections import defaultdict

from hogline.adapters.sources import digest
from hogline.reconstruction.concessions import resolve_concession

VERSION = "board-and-reviewed-ice-scores-v4"
CONCESSION_VERSION = "board-and-reviewed-ice-scores-v3"
PREFIX_VERSION = "board-and-reviewed-ice-scores-v2"
LEGACY_VERSION = "board-and-reviewed-ice-scores-v1"
COLORS = ("red", "yellow")


def board_evidence_ids(book):
    return {
        i for end in book.get("ends", []) for i in (end.get("board_evidence") or {}).get("evidence_ids", [])
    } | {c["evidence_id"] for c in book.get("current_end_evidence", []) if c.get("evidence_id")}


def policy_board_match(review, books):
    """Follow an expanded board history only with a unique retained evidence overlap."""
    old = review.get("board_history")
    if not old:
        return None
    matches = [
        key
        for key, book in books.items()
        if all(book.get(k) == old.get(k) for k in ("source_id", "source_metadata_sha256", "reader_version"))
        and book.get("first_seconds", float("inf")) <= old.get("first_seconds", -float("inf"))
        and book.get("last_seconds", -float("inf")) >= old.get("last_seconds", float("inf"))
        and len(board_evidence_ids(book) & board_evidence_ids(old)) >= 3
    ]
    return matches[0] if len(matches) == 1 else None


def reconcile_scores(report, reviews, *, version=VERSION):
    if version not in (VERSION, CONCESSION_VERSION, PREFIX_VERSION, LEGACY_VERSION):
        raise ValueError("Unsupported combined score history version")
    source_id = report["source_id"]
    relevant = [r for r in reviews if r["source_id"] == source_id]
    superseded = {i for r in relevant for i in r["request"]["supersedes_review_ids"]}
    active = [r for r in relevant if r["id"] not in superseded]
    books = {h["id"]: h for h in report["scorecards"]}
    groups = defaultdict(list)
    matched = {}
    for review in active:
        request = review["request"]
        if request.get("score_policy") and version != VERSION:
            raise ValueError("Score policies require combined score history v4")
        key = request["board_history_id"] or f"ice:{request['game_key']}"
        if request["board_history_id"] and key not in books:
            key = policy_board_match(review, books) if request.get("score_policy") else None
            if key is None:
                continue
        matched[review["id"]] = key
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
            decisions = [
                {
                    "review_id": r["id"],
                    **resolve_concession(
                        r["request"]["concession"],
                        {c: r["request"][f"{c}_points"] for c in COLORS},
                        totals,
                    ),
                }
                for r in observations
                if version in (VERSION, CONCESSION_VERSION) and r["request"].get("concession")
            ]
            if number != max([*board, *by_end], default=0):
                decisions = [
                    {**d, "status": "unresolved", "points": None, "reason": "later_end_in_same_history"}
                    for d in decisions
                ]
            final_claims = [
                r
                for r in observations
                if r["request"].get("end_completed") is True
                and r["request"]["phase"] != "unresolved"
                and r["request"]["red_points"] is not None
                and not r["request"].get("score_policy")
            ]
            lying_claims = {
                (r["request"]["red_points"], r["request"]["yellow_points"])
                for r in observations
                if r["request"]["red_points"] is not None and not r["request"].get("score_policy")
            }
            policies = [
                {
                    "review_id": r["id"],
                    **r["request"]["score_policy"],
                    "points": {c: r["request"][f"{c}_points"] for c in COLORS},
                }
                for r in observations
                if r["request"].get("score_policy")
            ]
            estimates = {tuple(p["points"][c] for c in COLORS) for p in policies}
            estimate = dict(zip(COLORS, next(iter(estimates)), strict=True)) if len(estimates) == 1 else None
            claims = {(r["request"]["red_points"], r["request"]["yellow_points"]) for r in final_claims}
            claims.update(tuple(d["points"][c] for c in COLORS) for d in decisions if d["status"] == "scored")
            unscored = any(d["status"] == "not_scored" for d in decisions)
            ice_score = dict(zip(COLORS, next(iter(claims)), strict=True)) if len(claims) == 1 else None
            disagreement = (
                (
                    unscored
                    and (
                        board_score is not None
                        or bool(claims)
                        or any(r["request"].get("end_completed") is True for r in observations)
                    )
                )
                or len(claims) > 1
                or bool(board_score is not None and ice_score is not None and board_score != ice_score)
                or (board_score is None and ice_score is None and len(estimates) > 1)
            )
            actual = board_score if board_score is not None else ice_score
            points = None if disagreement else actual if actual is not None else estimate
            is_estimate = bool(points is not None and actual is None and policies)
            estimate_basis = (
                "recording_cutoff_blank"
                if any(p["kind"] == "recording_cutoff_blank" for p in policies)
                else "lower_bound_estimate"
            )
            if points is None and not (unscored and not disagreement):
                totals = dict.fromkeys(COLORS, None)
            elif points is not None:
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
                    else "concession_not_scored"
                    if unscored
                    else "agreement"
                    if board_score is not None and ice_score is not None
                    else "scoreboard_corrected_estimate"
                    if board_score is not None and policies and board_score != estimate
                    else "scoreboard_confirms_estimate"
                    if board_score is not None and policies
                    else "scoreboard_only"
                    if board_score is not None
                    else "ice_only"
                    if ice_score is not None
                    else estimate_basis
                    if is_estimate
                    else "unresolved",
                    "status": "disputed"
                    if disagreement
                    else "not_scored"
                    if unscored
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
                    "state": "not_scored_concession"
                    if unscored and not disagreement
                    else "unknown"
                    if points is None
                    else "blank"
                    if not any(points.values())
                    else "played",
                }
            )
            if version == VERSION:
                ends[-1].update(
                    score_is_estimate=is_estimate,
                    estimated_points=estimate,
                    score_policy_applications=[
                        {
                            **p,
                            "decision": "used_estimate"
                            if is_estimate
                            else "unresolved_conflict"
                            if disagreement
                            else "replaced_by_actual_score",
                        }
                        for p in policies
                    ],
                )
            if version in (VERSION, CONCESSION_VERSION) and decisions:
                ends[-1]["concession_decisions"] = decisions
        completion_claims = [
            r
            for r in groups[key]
            if r["request"]["phase"] == "game_finished"
            and (
                r["request"].get("end_completed") is True
                or any(
                    d["review_id"] == r["id"] and d["status"] in ("scored", "not_scored")
                    for e in ends
                    for d in e.get("concession_decisions", [])
                )
            )
            and (r["request"]["official_end_number"] or r["request"]["observed_end_index"]) == len(ends)
        ]
        complete = bool(
            ends
            and completion_claims
            and all(
                e["status"] == "proposed" or (i == len(ends) - 1 and e["status"] == "not_scored")
                for i, e in enumerate(ends)
            )
        )
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
            if version != LEGACY_VERSION
            else len(ends)
            if ends and all(e["status"] == "proposed" for e in ends)
            else None,
            "game_completion": "complete" if complete else "unknown",
            "ends_played": len(ends)
            if complete and any(r["request"].get("end_completed") is True for r in completion_claims)
            else None,
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
        if version == VERSION:
            history["result_is_estimate"] = bool(complete and any(e["score_is_estimate"] for e in ends))
            history["review_board_history_associations"] = [
                {
                    "review_id": r["id"],
                    "original_history_id": r["request"]["board_history_id"],
                    "current_history_id": key,
                }
                for r in groups[key]
                if r["request"]["board_history_id"] and r["request"]["board_history_id"] != key
            ]
            history["id"] = digest({k: v for k, v in history.items() if k != "id"})
        histories.append(history)
    document = {
        "version": version,
        "source_id": source_id,
        "histories": histories,
        "unmatched_review_ids": sorted(
            r["id"] for r in active if r["request"]["board_history_id"] and r["id"] not in matched
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
