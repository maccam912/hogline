"""Reviewed called-line and short post-release path observations."""

from typing import Literal

from pydantic import Field, model_validator

from hogline.domain.models import Model


class AimObservation(Model):
    calibration_id: str = Field(min_length=1, max_length=160)
    calibration_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    track_id: str = Field(min_length=1, max_length=160)
    release_seconds: float = Field(ge=0)
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(gt=0)
    call_seconds: float = Field(ge=0)
    # All three points use the named calibration's fixed sheet-meter coordinates.
    line_origin_m: list[float] = Field(min_length=2, max_length=2)
    broom_m: list[float] = Field(min_length=2, max_length=2)
    right_side_m: list[float] = Field(min_length=2, max_length=2)
    reviewed_free_travel: bool = False
    reviewed_stone_association: bool = False
    reviewed_called_line: bool = False
    acceptance: Literal["proposed", "confirmed"] = "proposed"
    basis: str = Field(min_length=1, max_length=2000)
    evidence: list[str] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def bounded(self):
        if not self.release_seconds <= self.start_seconds < self.end_seconds <= self.release_seconds + 1.5:
            raise ValueError("Aim needs a post-release window within the first 1.5 seconds")
        if not 0.15 <= self.end_seconds - self.start_seconds <= 1.000001:
            raise ValueError("Aim window must span 0.15 to 1 second")
        if not self.release_seconds - 120 <= self.call_seconds <= self.release_seconds:
            raise ValueError("Called-line evidence must precede release by at most 120 seconds")
        if any(abs(v) > 1000 for p in (self.line_origin_m, self.broom_m, self.right_side_m) for v in p):
            raise ValueError("Called-line coordinates must use local sheet meters")
        if self.acceptance == "confirmed" and not all(
            (self.reviewed_free_travel, self.reviewed_stone_association, self.reviewed_called_line)
        ):
            raise ValueError(
                "Confirm the called line, delivered stone and free unswept travel before accepting aim"
            )
        return self
