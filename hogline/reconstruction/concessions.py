"""Explicit World Curling concession decisions from reviewed remaining-stone facts."""

RULE = "world-curling-2025-R12h"
REFERENCE = "https://worldcurling.org/wp-content/uploads/2025/08/Rules-2025.pdf#page=19"


def resolve_concession(facts, lying, previous):
    base = {"rule": RULE, "reference": REFERENCE, "facts": facts, "points": None}

    def result(status, reason, points=None):
        return {**base, "status": status, "reason": reason, "points": points}

    if not facts or facts.get("rule") != RULE:
        return result("unresolved", "concession_rule_not_selected")
    remaining = {c: facts.get(f"{c}_remaining") for c in ("red", "yellow")}
    if any(v is None for v in remaining.values()):
        return result("unresolved", "remaining_stones_unknown")
    if all(v > 0 for v in remaining.values()):
        return result("not_scored", "both_teams_have_stones_remaining")
    if not any(remaining.values()):
        return result("unresolved", "all_stones_played_use_completed_end_review")
    if lying is None or any(lying.get(c) is None for c in remaining):
        return result("unresolved", "lying_score_unknown")
    scoring = next((c for c in remaining if lying[c] > 0), None)
    if scoring is None:
        return result("not_scored", "no_stones_counting")
    if remaining[scoring] > 0:
        return result("scored", "counting_team_has_last_stone_remaining", lying)
    opponent = "yellow" if scoring == "red" else "red"
    if previous is None or any(previous.get(c) is None for c in remaining):
        return result("unresolved", "prior_score_needed_for_outcome_exception")
    if previous[scoring] > previous[opponent]:
        return result("not_scored", "counting_team_finished_its_stones_and_already_leads")
    return result("scored", "points_required_to_determine_outcome", lying)
