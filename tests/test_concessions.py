import pytest

from hogline.reconstruction.concessions import RULE, resolve_concession


@pytest.mark.parametrize(
    "remaining,lying,previous,status,points",
    [
        ((2, 1), (0, 4), (1, 6), "not_scored", None),
        ((1, 0), (0, 4), (1, 6), "not_scored", None),
        ((0, 1), (0, 4), (6, 3), "scored", (0, 4)),
        ((1, 0), (0, 4), (6, 3), "scored", (0, 4)),
        ((1, 0), (0, 4), (3, 3), "scored", (0, 4)),
        ((1, 0), (0, 4), (8, 2), "unresolved", None),
        ((1, 0), (0, 4), (6, 2), "unresolved", None),
        ((1, 0), (0, 0), (1, 6), "not_scored", None),
        ((None, 0), (0, 4), (1, 6), "unresolved", None),
        ((1, 0), (None, None), (1, 6), "unresolved", None),
        ((1, 0), (0, 4), (None, None), "unresolved", None),
        ((0, 0), (0, 4), (1, 6), "unresolved", None),
    ],
)
def test_remaining_stones_and_outcome_exception_control_concession_score(
    remaining, lying, previous, status, points
):
    facts = {
        "rule": RULE,
        "conceding_color": "yellow" if remaining[0] == 0 and remaining[1] else "red",
        "red_remaining": remaining[0],
        "yellow_remaining": remaining[1],
    }
    decision = resolve_concession(
        facts,
        dict(zip(("red", "yellow"), lying, strict=True)),
        dict(zip(("red", "yellow"), previous, strict=True)),
    )
    assert decision["status"] == status
    assert decision["points"] == (dict(zip(("red", "yellow"), points, strict=True)) if points else None)


def test_unselected_club_rule_cannot_apply_standard_concession_points():
    assert (
        resolve_concession(None, {"red": 0, "yellow": 4}, {"red": 1, "yellow": 6})["status"] == "unresolved"
    )
