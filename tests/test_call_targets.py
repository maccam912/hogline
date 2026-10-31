import pytest
from pydantic import ValidationError

from hogline.perception.call_targets import CallEvidenceProposal, TargetPoint, source_point, validate_targets


def target(**changes):
    return {
        "frame": 1,
        "kind": "called_line",
        "point": [0.2, 0.7],
        "actor_tag": None,
        "evidence_basis": "Broom head planted at indicated line",
        "limitations": [],
        **changes,
    }


def proposal(**changes):
    return {
        "call": "unknown",
        "call_basis": "Intent not established",
        "broom_visible": True,
        "delivery_visible": False,
        "sweeping_visible": False,
        "observations": [],
        "limitations": [],
        "deliveries": [],
        "activities": [],
        "targets": [target()],
        "strategy_options": [],
        **changes,
    }


def test_target_bounds_frame_and_actor_links_require_visible_evidence():
    for point in ([float("nan"), 0.1], [0, float("inf")], [-0.1, 0.2], [1.1, 0]):
        with pytest.raises(ValidationError):
            TargetPoint.model_validate(target(point=point))
    parsed = CallEvidenceProposal.model_validate(proposal())
    with pytest.raises(ValueError, match="not supplied"):
        validate_targets(parsed, 1)
    with pytest.raises(ValueError, match="Target holder"):
        CallEvidenceProposal.model_validate(proposal(targets=[target(actor_tag="A1")]))


def test_strategy_hypothesis_does_not_establish_call_or_resting_target():
    parsed = CallEvidenceProposal.model_validate(
        proposal(
            strategy_options=[
                {
                    "call": "takeout",
                    "rationale": "Remove the exposed scoring stone",
                    "missing_context": ["Hammer and end unknown"],
                }
            ]
        )
    )
    assert parsed.call == "unknown"
    assert parsed.targets[0].kind == "called_line"
    assert parsed.strategy_options[0].call == "takeout"
    with pytest.raises(ValueError):
        CallEvidenceProposal.model_validate(
            proposal(
                strategy_options=[
                    {
                        "call": "takeout",
                        "rationale": "Possible",
                        "missing_context": [],
                    }
                ]
            )
        )


def test_point_mapping_uses_actual_crop_pixels_without_inventing_ice_coordinates():
    result = source_point([0.25, 0.75], [720, 600, 1200, 1080], [1920, 1080])
    assert result["source_pixel_xy"] == [840, 960]
    assert result["source_normalized_xy"] == pytest.approx([0.4375, 8 / 9])


def test_visible_broom_tip_can_be_retained_without_inventing_a_call():
    parsed = CallEvidenceProposal.model_validate(proposal(targets=[target(kind="broom_tip_unknown_purpose")]))
    assert parsed.call == "unknown"
    assert parsed.targets[0].kind == "broom_tip_unknown_purpose"
