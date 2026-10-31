"""Timestamped contact sheets and structured shot/role observations for short sequences."""

import math
from typing import Literal

from PIL import Image, ImageDraw
from pydantic import Field, model_validator

from hogline.domain.models import Model
from hogline.perception.vision_calls import PROMPT, CallProposal

VERSION = "curling-sequence-v4"
SEQUENCE_PROMPT = (
    PROMPT
    + """
These images are chronological contact sheets from ONE short source interval. Tiles are
numbered F0, F1, etc. Composite tiles have several camera views of the same instant;
cropped tiles show only the view named in the label. Count a
physical delivery only once across views. Use zero-based frame indices shown on the tiles.
Distinguish a curling delivery from sweeping, cleaning or moving a stopped stone.
A delivery can be a conventional slide OR an upright/standing delivery using a delivery
stick attached to the stone handle, or a standing hand delivery. Do NOT require a crouch,
slide, or direct hand-to-stone contact. For a stick delivery, look for the person guiding
the stone forward from the delivery end, the stick disengaging from its handle, and the
stone continuing independently toward play. Walking behind a moving stone alone is
insufficient. Distinguish this from nudging/repositioning parked stones during cleanup.
Return deliveries only when controlled delivery motion or release is supported across
frames. Describe the visible delivery method in the thrower description/evidence basis;
do not infer why a person uses a method. Do not invent team delivery number, end number, game
role, named identity, stone speed or accuracy. Give a frame-local thrower description.
For each delivery: first_frame and last_frame bound the observed delivery action;
release_frame is the first frame after hand OR delivery-stick separation from the stone,
or null when unseen. An upright stick-delivery participant is the thrower actor, not merely
a walking observer. Keep release null if the tool/handle interaction is obscured.
stone_color is red/yellow/unknown. Call may remain unknown. A broom point, if clear,
uses normalized x/y in the ORIGINAL IMAGE TILE, excluding its label bar and letterbox; broom_frame
must be a pre-release frame. The point is the visible head/tip touching ice, not the handle
held in the air. If ambiguous or hidden, return null for broom_frame and broom_tip.
The top-level call/observations summarize the interval. Include deliveries as an array.
Never infer skip or throwing position solely from standing in a house or throwing last.

Also return activities for clearly visible participants, using at most 12 concise entries.
Assign anonymous actor_tag values A1, A2, etc. within THIS interval only. Reuse a tag only
when clothing, location and continuous movement support the same participant; split when
uncertain. Do not identify, name, or recognize anyone, and do not join people across clips.
Describe clothing/location, without demographic guesses. Use activity categories delivery,
sweeping, calling_broom, cleaning, handling_stone, walking, waiting, or unknown.
Calling_broom requires evidence of deliberately showing a line/target, not just holding a
broom upright. Sweeping requires movement near a moving stone, not cleaning parked stones.
first_frame/last_frame bound the observed activity, not continuous activity through gaps.
key_frame is an observed frame inside that interval; bbox is normalized [left,top,right,bottom]
around that participant in the ORIGINAL IMAGE TILE, excluding label bars and letterbox.
For composite tiles choose one camera view, never a box spanning duplicate camera views.
team_side is red/yellow/unknown, with a visible side_basis. Clothing color is NOT team color;
sweeping a stone alone cannot establish membership, since opponents may sweep behind the tee.
Keep side unknown unless the delivery/interaction establishes it. stone_color describes the
interacted stone separately. delivery_index links to a returned delivery only when established;
otherwise null. Each delivery's thrower_actor_tag must link to a delivery activity or be null.
Keep descriptions and evidence_basis concise. Never treat an activity as a confirmed game role.
"""
)


class DeliveryProposal(Model):
    first_frame: int = Field(ge=0, le=15)
    last_frame: int = Field(ge=0, le=15)
    release_frame: int | None = Field(ge=0, le=15)
    stone_color: Literal["red", "yellow", "unknown"]
    thrower_description: str = Field(min_length=1, max_length=1000)
    call: Literal["unknown", "draw", "guard", "freeze", "takeout", "peel", "raise"]
    call_basis: str = Field(max_length=2000)
    broom_frame: int | None = Field(ge=0, le=15)
    broom_tip: list[float] | None = Field(min_length=2, max_length=2)
    limitations: list[str] = Field(max_length=20)

    @model_validator(mode="after")
    def ordered(self):
        if self.first_frame >= self.last_frame:
            raise ValueError("Delivery frame interval is reversed")
        if self.release_frame is not None and not self.first_frame <= self.release_frame <= self.last_frame:
            raise ValueError("Release must lie inside the observed delivery interval")
        if (self.broom_frame is None) != (self.broom_tip is None):
            raise ValueError("Broom point needs a frame and normalized coordinates")
        if self.broom_tip and any(not math.isfinite(v) or not 0 <= v <= 1 for v in self.broom_tip):
            raise ValueError("Broom point must be inside the source image")
        if self.broom_frame is not None:
            before = self.release_frame if self.release_frame is not None else self.first_frame
            if self.broom_frame >= before:
                raise ValueError("A called broom needs observed pre-release evidence")
        return self


class SequenceProposal(CallProposal):
    deliveries: list[DeliveryProposal] = Field(max_length=8)


class ActivityProposal(Model):
    actor_tag: str = Field(pattern=r"^A[1-9][0-9]?$", max_length=3)
    description: str = Field(min_length=1, max_length=500)
    first_frame: int = Field(ge=0, le=15)
    last_frame: int = Field(ge=0, le=15)
    key_frame: int = Field(ge=0, le=15)
    bbox: list[float] = Field(min_length=4, max_length=4)
    action: Literal[
        "delivery", "sweeping", "calling_broom", "cleaning", "handling_stone", "walking", "waiting", "unknown"
    ]
    team_side: Literal["red", "yellow", "unknown"]
    side_basis: str = Field(max_length=1000)
    stone_color: Literal["red", "yellow", "unknown"]
    delivery_index: int | None = Field(ge=0, le=7)
    evidence_basis: str = Field(min_length=1, max_length=1000)
    limitations: list[str] = Field(max_length=10)

    @model_validator(mode="after")
    def observed(self):
        if not self.first_frame <= self.key_frame <= self.last_frame:
            raise ValueError("Actor key frame must be inside its observed interval")
        if self.action in ("delivery", "sweeping") and self.first_frame == self.last_frame:
            raise ValueError("Delivery or sweeping activity needs multiple observed frames")
        x1, y1, x2, y2 = self.bbox
        if not 0 <= x1 < x2 <= 1 or not 0 <= y1 < y2 <= 1:
            raise ValueError("Actor bbox must have positive normalized area")
        if self.team_side != "unknown" and not self.side_basis.strip():
            raise ValueError("A team-side proposal needs visible evidence")
        return self


class ActorDeliveryProposal(DeliveryProposal):
    thrower_actor_tag: str | None = Field(pattern=r"^A[1-9][0-9]?$", max_length=3)


class ActivitySequenceProposal(SequenceProposal):
    deliveries: list[ActorDeliveryProposal] = Field(max_length=8)
    activities: list[ActivityProposal] = Field(max_length=12)

    @model_validator(mode="after")
    def actor_links(self):
        for activity in self.activities:
            if activity.delivery_index is not None and activity.delivery_index >= len(self.deliveries):
                raise ValueError("Activity links to an absent delivery")
        for index, delivery in enumerate(self.deliveries):
            if delivery.thrower_actor_tag is None:
                continue
            matches = [
                a
                for a in self.activities
                if a.actor_tag == delivery.thrower_actor_tag
                and a.action == "delivery"
                and a.delivery_index == index
            ]
            if not matches or not any(
                max(a.first_frame, delivery.first_frame) <= min(a.last_frame, delivery.last_frame)
                for a in matches
            ):
                raise ValueError("Thrower needs a linked, overlapping delivery activity")
        return self


def contact_sheets(frames, view="composite"):
    """Up to 16 composite frames, four per image; preserve readable time labels."""
    if not 2 <= len(frames) <= 16:
        raise ValueError("Sequence requires 2–16 ordered frames")
    if any(a[0] >= b[0] for a, b in zip(frames, frames[1:], strict=False)):
        raise ValueError("Sequence frame times must increase strictly")
    sheets = []
    for start in range(0, len(frames), 4):
        selected = frames[start : start + 4]
        tile_height = 288 if view == "composite" else 480
        row_height = tile_height + 32
        sheet = Image.new("RGB", (1024, row_height * 2), (24, 29, 27))
        draw = ImageDraw.Draw(sheet)
        for offset, (seconds, frame) in enumerate(selected):
            image = frame.convert("RGB")
            image.thumbnail((512, tile_height))
            x, y = (offset % 2) * 512, (offset // 2) * row_height
            sheet.paste(image, (x, y + 32))
            draw.text((x + 8, y + 8), f"F{start + offset} | source {seconds:.3f}s", fill="white")
        sheets.append(
            (
                f"View {view}; chronological F{start}–F{start + len(selected) - 1}; labels are source times",
                sheet,
            )
        )
    return sheets


def validate_frame_references(proposal, count):
    for activity in getattr(proposal, "activities", []):
        if max(activity.first_frame, activity.last_frame, activity.key_frame) >= count:
            raise ValueError("Activity refers to a frame not supplied")
    for delivery in proposal.deliveries:
        for index in (
            delivery.first_frame,
            delivery.last_frame,
            delivery.release_frame,
            delivery.broom_frame,
        ):
            if index is not None and index >= count:
                raise ValueError("Vision proposal refers to a frame not supplied")
