"""Explain recovered scores and remaining work without assuming a game's length."""


def result_review(report, reviews=()):
    boards = report.get("scorecard_reconstruction", {}).get("scorecards", [])
    by_id = {book["id"]: book for book in boards}
    histories = report.get("reconciled_score_history", {}).get("histories") or boards
    if not histories:
        histories = [book for book in report.get("board_ledgers", []) if book.get("ends")]
    summaries = []
    known = complete = 0
    for history in histories:
        ends = history.get("ends", [])
        scored = [e for e in ends if all(e.get("points", {}).get(c) is not None for c in ("red", "yellow"))]
        known += len(scored)
        finished = history.get("game_completion") == "complete"
        board = by_id.get(history.get("board_history_id", history.get("id")), {})
        missing = [
            e.get("official_number") or e.get("observed_index")
            for e in ends
            if e not in scored and e.get("state") != "not_scored_concession"
        ]
        resolved_numbers = {e.get("official_number") or e.get("observed_index") for e in scored}
        pending = [n for n in board.get("pending_end_numbers", []) if n not in resolved_numbers]
        blockers = []
        if missing or pending:
            blockers.append(
                {
                    "code": "unresolved_end_scores",
                    "end_numbers": sorted(set(missing + pending)),
                    "message": "Review missing end cards and the settled house before clearing; "
                    "missing cards are not blanks.",
                }
            )
        if board.get("card_conflicts"):
            blockers.append(
                {
                    "code": "conflicting_board_readings",
                    "message": "Review competing repeated card readings within this board history.",
                }
            )
        if not finished:
            blockers.append(
                {
                    "code": "game_ending_unreviewed",
                    "message": "Review the last played end and what follows: "
                    "further play, handshakes, departure or awards. "
                    "Games may be 6, 8 or 10 ends; a posted end number alone does not establish completion.",
                }
            )
        complete += finished and not blockers
        anchors = [e["board_evidence"]["first"] for e in ends if (e.get("board_evidence") or {}).get("first")]
        summaries.append(
            {
                "history_id": history["id"],
                "board_history_id": history.get("board_history_id", history.get("id")),
                "known_end_scores": len(scored),
                "uncertain_end_numbers": history.get("uncertain_end_numbers", []),
                "final_result_range": history.get("final_result_range"),
                "game_completion": history.get("game_completion", "unknown"),
                "scheduled_ends": None,
                "blockers": blockers,
                "last_score_evidence": anchors[-1] if anchors else None,
            }
        )
    partial = len(report.get("partial_score_proposals", []))
    status = (
        "complete_results"
        if histories and complete == len(histories)
        else "scores_extracted"
        if known
        else "partial_scores"
        if partial
        else "no_scores"
    )
    superseded = {i for review in reviews for i in review["request"].get("supersedes_review_ids", [])}
    return {
        "status": status,
        "known_end_scores": known,
        "partial_score_claims": partial,
        "proposed_complete_games": complete,
        "candidate_games": len(histories),
        "histories": summaries,
        "reviews": [
            {
                "id": review["id"],
                "board_history_id": review["request"].get("board_history_id"),
                "official_end_number": review["request"].get("official_end_number"),
                "phase": review["request"]["phase"],
                "reviewer_kind": review["request"]["reviewer_kind"],
                "note": review["request"]["note"],
                "evidence": [
                    {"evidence_id": identifier, "source_seconds": pin["source_seconds"]}
                    for identifier, pin in sorted(
                        review["evidence_refs"].items(), key=lambda item: item[1]["source_seconds"]
                    )
                ],
            }
            for review in reviews
            if review["id"] not in superseded
        ],
    }
