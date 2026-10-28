from typing import Literal

from pydantic import Field, model_validator

from hogline.domain.models import Model, uid


class LabelPoint(Model):
    id: str = Field(default_factory=uid)
    color: Literal["red", "yellow", "unknown"]
    center_px: tuple[float, float]
    visibility: Literal["visible", "partial"] = "visible"
    note: str = Field(default="", max_length=1000)


class IgnoreRegion(Model):
    x: float = Field(ge=0)
    y: float = Field(ge=0)
    width: float = Field(gt=0)
    height: float = Field(gt=0)
    reason: str = Field(default="occluded_or_unobservable", min_length=1, max_length=300)


class Labels(Model):
    evidence_id: str
    view: Literal["house_top", "house_bottom"]
    object_kind: Literal["colored_handle_center"] = "colored_handle_center"
    coverage: Literal["partial", "complete_visible", "unobservable"] = "partial"
    review_status: Literal["draft", "reviewed"] = "draft"
    provenance: Literal["human", "assistant_visual", "synthetic", "model_seeded"] = "human"
    evaluation_group: str = Field(min_length=1, max_length=200)
    points: list[LabelPoint] = Field(default_factory=list, max_length=128)
    ignore_regions: list[IgnoreRegion] = Field(default_factory=list, max_length=64)
    negative_regions: list[IgnoreRegion] = Field(default_factory=list, max_length=64)
    copied_from_model_version: str | None = None
    note: str = Field(default="", max_length=5000)

    @model_validator(mode="after")
    def truthful_coverage(self):
        if len({p.id for p in self.points}) != len(self.points):
            raise ValueError("Label point IDs must be unique")
        if self.provenance == "model_seeded" and self.review_status == "reviewed":
            raise ValueError("Unverified model seeds cannot be reviewed ground truth")
        if self.coverage == "unobservable" and self.points:
            raise ValueError("An unobservable view cannot also contain measured handle centers")
        if not self.evaluation_group.strip():
            raise ValueError("An evaluation group is required to prevent split leakage")
        return self


class LabelEdit(Model):
    expected_revision: int = Field(ge=0)
    author: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=1, max_length=2000)
    labels: Labels


class DatasetCreate(Model):
    name: str = Field(min_length=1, max_length=200)
    author: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=1, max_length=2000)
    mode: Literal["confirmed", "exploratory"] = "confirmed"
    partitions: dict[str, Literal["train", "validation", "test"]]


class EvaluationRequest(Model):
    dataset_id: str
    model_version: str | None = None
    partition: Literal["train", "validation", "test"] = "test"
    max_center_error_fraction: float = Field(default=0.035, gt=0, le=0.2)
