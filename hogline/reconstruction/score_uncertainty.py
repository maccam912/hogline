"""Plausible end scores and cumulative bounds; no invented confidence percentages."""

COLORS = ("red", "yellow")


def score_bounds(options):
    return (
        {c: {"min": min(o[c] for o in options), "max": max(o[c] for o in options)} for c in COLORS}
        if options
        else None
    )


def add_uncertainty(history):
    """Propagate alternatives without losing the correlation between team scores.

    An unknown end invalidates later bounds. A conceded, unscored end preserves
    them. Board corrections supply a singleton and remove earlier uncertainty.
    """
    totals = {(0, 0)}
    for end in history["ends"]:
        options = end.get("score_options")
        if options is None:
            points = end["points"]
            options = [points] if all(points.get(c) is not None for c in COLORS) else []
            end["score_options"] = options
        end["points_range"] = score_bounds(options)
        end["score_confidence"] = (
            "scoreboard"
            if end.get("board_points") is not None
            else "uncertain"
            if len(options) > 1
            else "ice_estimate"
            if options
            else "unknown"
        )
        if end["status"] != "not_scored":
            totals = {
                (red + option["red"], yellow + option["yellow"])
                for red, yellow in totals
                for option in options
            }
        end["score_after_end_range"] = score_bounds([{"red": r, "yellow": y} for r, y in totals])
    complete = history["game_completion"] == "complete"
    history["final_result_range"] = history["ends"][-1]["score_after_end_range"] if complete else None
    history["possible_outcomes"] = (
        sorted({"red_win" if r > y else "yellow_win" if y > r else "tie" for r, y in totals})
        if complete
        else []
    )
    history["uncertain_end_numbers"] = [
        e["official_number"] or e["observed_index"]
        for e in history["ends"]
        if len(e["score_options"]) > 1 or e["status"] in ("disputed", "unresolved")
    ]
