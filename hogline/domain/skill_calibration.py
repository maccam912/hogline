"""Reviewed, source-bound camera-plane landmark sets."""

from typing import Literal

from pydantic import Field, model_serializer, model_validator

from hogline.domain.models import Model


class Landmark(Model):
    label: str = Field(min_length=1, max_length=100)
    pixel: list[float] = Field(min_length=2, max_length=2)
    plane_m: list[float] = Field(min_length=2, max_length=2)
    use: Literal["fit", "validation"] = "fit"


class CalibrationTransfer(Model):
    parent_id: str = Field(min_length=1, max_length=160)
    parent_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    target_landmarks_reviewed: bool = False


class CameraCalibration(Model):
    id: str = Field(min_length=1, max_length=160)
    motion_id: str
    motion_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_id: str
    view: Literal["house_top", "house_bottom", "perspective_left", "perspective_right"]
    reference_sample_index: int = Field(default=-1, ge=-1, le=159)
    image_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(gt=0)
    landmarks: list[Landmark] = Field(default_factory=list, max_length=32)
    # Explicit convention prevents a mirrored display from silently changing aim signs.
    coordinate_system: str = Field(min_length=1, max_length=500)
    dimension_basis: Literal["nominal_rules", "site_measured"] = "nominal_rules"
    max_validation_error_m: float = Field(default=0.05, ge=0.001, le=0.25)
    acceptance: Literal["proposed", "confirmed", "excluded"] = "proposed"
    basis: str = Field(min_length=1, max_length=2000)
    evidence: list[str] = Field(default_factory=list, max_length=32)
    transfer: CalibrationTransfer | None = None

    @model_serializer(mode="wrap")
    def preserve_original_checksum(self, handler):
        document = handler(self)
        if self.transfer is None:
            document.pop("transfer", None)
        return document

    @model_validator(mode="after")
    def bounded(self):
        if self.end_seconds <= self.start_seconds:
            raise ValueError("Calibration interval must have positive duration")
        if len({p.label for p in self.landmarks}) != len(self.landmarks):
            raise ValueError("Calibration landmark labels must be unique")
        if any(abs(v) > 1000 for p in self.landmarks for v in p.plane_m):
            raise ValueError("Calibration coordinates must be local sheet meters")
        if self.acceptance == "confirmed" and not self.evidence:
            raise ValueError("Confirmed calibration needs dimension and landmark evidence")
        return self
