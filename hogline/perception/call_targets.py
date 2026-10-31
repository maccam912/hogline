"""Visible pre-delivery targets, kept separate from tactical hypotheses."""

import math
from typing import Literal

from pydantic import Field, model_validator

from hogline.domain.models import Model
from hogline.perception.shot_sequence import SEQUENCE_PROMPT, ActivitySequenceProposal

VERSION = "pre-delivery-call-evidence-v3"
PROMPT = (
    SEQUENCE_PROMPT
    + """
This interval was selected BEFORE a separate proposed delivery, to inspect call evidence.
Only the named camera view is supplied. The receiving end is NOT yet established; do not
assume this is the receiving house or that any gesture belongs to the later delivery.
Look closely for a deliberately planted calling broom or pointing at a stone/region.
Return targets only for visible deliberate call/pointing gestures, never parked brooms,
cleaning, active sweeping, or merely standing in a house. Distinguish called_line (broom
head touching ice) from pointed_stone/pointed_region (a visibly indicated tactical target).
A called-line broom is NOT a prediction of the desired resting position.
Each target has a frame, normalized point in the original view image, optional actor_tag,
visible evidence_basis and limitations. If no point is clearly visible return targets [].
Never extrapolate a hidden broom tip. Do not use the eventual stone finish as intent.
An exception for retaining raw evidence: if a broom head/tip is clearly visible but its
purpose is uncertain, retain a point with kind broom_tip_unknown_purpose. This is only
a visible object location, NOT a target, called line, proof of ice contact or game role.
Use this kind for a clearly visible stationary/resting broom whose intent is unclear.
Do not assign called_line merely because the broom stays still before the delivery.
Retain at most three representative points rather than one point for every frame.
The top-level call is an OBSERVATION-supported proposal; keep unknown without sufficient
visible evidence, even if a tactical choice seems obvious.
Separately, strategy_options may contain up to THREE plausible alternative calls based
on the visible arrangement. They are hypotheses, not observed calls or recommendations
with known game context. State a short rationale and missing_context (e.g. hammer, score,
end, throwing order, intended weight, obscured stones). Return [] when the house is too
unclear. Do not assign probabilities, player skills, speeds or physical accuracy.
"""
)


class TargetPoint(Model):
    frame: int = Field(ge=0, le=15)
    kind: Literal["called_line", "pointed_stone", "pointed_region", "broom_tip_unknown_purpose"]
    point: list[float] = Field(min_length=2, max_length=2)
    actor_tag: str | None = Field(pattern=r"^A[1-9][0-9]?$", max_length=3)
    evidence_basis: str = Field(min_length=1, max_length=1000)
    limitations: list[str] = Field(max_length=10)

    @model_validator(mode="after")
    def inside(self):
        if any(not math.isfinite(v) or not 0 <= v <= 1 for v in self.point):
            raise ValueError("Target point must be finite and inside the supplied view")
        return self


class StrategyOption(Model):
    call: Literal["draw", "guard", "freeze", "takeout", "peel", "raise"]
    rationale: str = Field(min_length=1, max_length=1000)
    missing_context: list[str] = Field(min_length=1, max_length=10)


class CallEvidenceProposal(ActivitySequenceProposal):
    targets: list[TargetPoint] = Field(max_length=8)
    strategy_options: list[StrategyOption] = Field(max_length=3)

    @model_validator(mode="after")
    def target_links(self):
        for target in self.targets:
            if target.actor_tag is not None and not any(
                a.actor_tag == target.actor_tag
                and (a.action == "calling_broom" or target.kind == "broom_tip_unknown_purpose")
                and a.first_frame <= target.frame <= a.last_frame
                for a in self.activities
            ):
                raise ValueError("Target holder needs an overlapping calling-broom activity")
        return self


def validate_targets(proposal, count):
    if any(t.frame >= count for t in proposal.targets):
        raise ValueError("Target refers to a frame not supplied")


def source_point(point, crop_bounds_px, source_size):
    """Map continuous view-normalized coordinates through the actual rounded crop."""
    x1, y1, x2, y2 = crop_bounds_px
    x, y = x1 + point[0] * (x2 - x1), y1 + point[1] * (y2 - y1)
    return {"source_pixel_xy": [x, y], "source_normalized_xy": [x / source_size[0], y / source_size[1]]}
