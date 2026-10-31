import math

import numpy as np
import pytest
from pydantic import ValidationError

from hogline.domain.skill_aim import AimObservation
from hogline.perception.shot_aim import signed_line_angle


@pytest.mark.parametrize(
    "transform",
    [
        [[1, 0], [0, 1]],
        [[-1, 0], [0, -1]],
        [[-1, 0], [0, 1]],
        [[0, 1], [1, 0]],
        [[0.6, -0.8], [0.8, 0.6]],
    ],
)
def test_aim_sign_survives_reverse_travel_rotated_axes_and_mirrored_map(transform):
    transform = np.array(transform)
    called = transform @ [0, 30]
    right = transform @ [1, 0]
    assert signed_line_angle(transform @ [0.2, 2], called, right) == pytest.approx(
        math.degrees(math.atan(0.1))
    )
    assert signed_line_angle(transform @ [-0.2, 2], called, right) == pytest.approx(
        -math.degrees(math.atan(0.1))
    )
    assert signed_line_angle(transform @ [0, 2], called, right) == pytest.approx(0)


def test_aim_refuses_reversed_motion_and_ambiguous_right_marker():
    with pytest.raises(ValueError, match="called end"):
        signed_line_angle([0, -2], [0, 30], [1, 0])
    with pytest.raises(ValueError, match="right-side marker"):
        signed_line_angle([0, 2], [0, 30], [0, 4])


def observation(**changes):
    return AimObservation.model_validate(
        dict(
            calibration_id="cal1",
            calibration_sha256="a" * 64,
            track_id="path",
            release_seconds=9.9,
            start_seconds=10,
            end_seconds=10.5,
            call_seconds=9,
            line_origin_m=[0, 0],
            broom_m=[0, 30],
            right_side_m=[1, 0],
            basis="Reviewed synthetic line",
            evidence=["fixture"],
        )
        | changes
    )


@pytest.mark.parametrize(
    "change",
    [
        {"start_seconds": 9.8},
        {"end_seconds": 11.5},
        {"end_seconds": 10.1},
        {"call_seconds": 10},
        {"acceptance": "confirmed"},
        {"right_side_m": [float("nan"), 0]},
    ],
)
def test_unreviewed_or_bad_time_geometry_is_not_confirmed(change):
    with pytest.raises(ValidationError):
        observation(**change)
