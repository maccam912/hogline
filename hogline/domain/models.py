import math
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

Acceptance = Literal["proposed", "human_confirmed", "disputed", "unknown"]


def uid():
    return str(uuid4())


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Span(Model):
    source_id: str
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(gt=0)
    relationship: Literal["primary", "overlap", "continuation", "alternate_view", "unresolved"] = "primary"
    boundary_uncertainty_seconds: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def ordered(self):
        if self.end_seconds <= self.start_seconds:
            raise ValueError("A source span must end after it starts")
        return self


class End(Model):
    id: str = Field(default_factory=uid)
    observed_index: int = Field(ge=1)
    official_number: int | None = Field(default=None, ge=1)
    state: Literal["unknown", "played", "blank", "unplayed"] = "unknown"
    red_points: int | None = Field(default=None, ge=0, le=8)
    yellow_points: int | None = Field(default=None, ge=0, le=8)
    acceptance: Acceptance = "unknown"
    evidence_ids: list[str] = Field(default_factory=list)
    note: str = ""

    @model_validator(mode="after")
    def score_contract(self):
        r, y = self.red_points, self.yellow_points
        if self.state in ("unknown", "unplayed"):
            if r is not None or y is not None or self.acceptance == "human_confirmed":
                raise ValueError("Unknown/unplayed ends have null points and cannot be confirmed scores")
        else:
            if r is None or y is None:
                raise ValueError("A played or blank end needs both point values")
            if r > 0 and y > 0:
                raise ValueError("Only one team can score in an end")
            if (self.state == "blank") != (r == 0 and y == 0):
                raise ValueError("Use blank for 0–0, played for a scoring end")
            if not self.evidence_ids:
                raise ValueError("A score needs at least one evidence frame")
        return self


class Result(Model):
    outcome: Literal["unknown", "red_win", "yellow_win", "tie", "forfeit"] = "unknown"
    red_points: int | None = Field(default=None, ge=0)
    yellow_points: int | None = Field(default=None, ge=0)
    acceptance: Acceptance = "unknown"
    evidence_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def valid_result(self):
        r, y = self.red_points, self.yellow_points
        if self.outcome == "unknown":
            if r is not None or y is not None or self.acceptance == "human_confirmed":
                raise ValueError("An unknown result must retain null points")
        else:
            if not self.evidence_ids:
                raise ValueError("A final result needs independent supporting evidence")
            if self.outcome != "forfeit":
                if r is None or y is None:
                    raise ValueError("A decided result needs both final scores")
                expected = "red_win" if r > y else "yellow_win" if y > r else "tie"
                if expected != self.outcome:
                    raise ValueError("Outcome conflicts with final scores")
        return self


class Game(Model):
    id: str = Field(default_factory=uid)
    sheet: int = Field(ge=1, le=8)
    label: str = Field(default="Untitled game", min_length=1, max_length=200)
    league: str | None = Field(default=None, max_length=200)
    red_team: str = Field(default="Red · unidentified", min_length=1, max_length=200)
    yellow_team: str = Field(default="Yellow · unidentified", min_length=1, max_length=200)
    acceptance: Acceptance = "proposed"
    coverage: Literal["unknown", "partial", "complete"] = "unknown"
    completion: Literal["unknown", "complete", "conceded", "forfeit"] = "unknown"
    spans: list[Span] = Field(min_length=1)
    ends: list[End] = Field(default_factory=list)
    result: Result = Field(default_factory=Result)
    note: str = Field(default="", max_length=10000)
    retired_into: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_ends(self):
        indices = [e.observed_index for e in self.ends]
        numbers = [e.official_number for e in self.ends if e.official_number is not None]
        if len(set(indices)) != len(indices) or len(set(numbers)) != len(numbers):
            raise ValueError("End indices and known official numbers must be unique")
        if len({e.id for e in self.ends}) != len(self.ends):
            raise ValueError("End IDs must be unique within a game")
        if self.result.outcome != "unknown" and self.completion == "unknown":
            raise ValueError("A final result requires explicit game completion")
        return self


class GameEdit(Model):
    expected_revision: int = Field(ge=0)
    author: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=1, max_length=2000)
    game: Game


class ScanRequest(Model):
    source_id: str
    start_seconds: float = Field(default=0, ge=0)
    end_seconds: float = Field(gt=0)
    interval_seconds: float = Field(default=10, ge=1, le=60)
    max_frames: int = Field(default=120, ge=2, le=1000)
    max_evidence_mb: int = Field(default=200, ge=1, le=512)
    max_worker_seconds: int = Field(default=600, ge=5, le=1800)
    acquisition_tier: Literal["coarse", "evidence"] = "evidence"
    propose_games: bool = False
    ownership_start_seconds: float | None = Field(default=None, ge=0)
    ownership_end_seconds: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def bounded(self):
        if self.end_seconds <= self.start_seconds:
            raise ValueError("Scan end must follow start")
        if self.end_seconds - self.start_seconds > 1200:
            raise ValueError("Work units are limited to 20 minutes; enqueue adjacent chunks")
        if math.ceil((self.end_seconds - self.start_seconds) / self.interval_seconds) > self.max_frames:
            raise ValueError("Scan exceeds the frame budget; shorten it or increase the interval")
        a, b = self.ownership_start_seconds, self.ownership_end_seconds
        if (a is None) != (b is None):
            raise ValueError("Both central ownership bounds are required together")
        if a is not None and not self.start_seconds <= a < b <= self.end_seconds:
            raise ValueError("Central ownership must be an ordered interval inside the context scan")
        return self

    @property
    def ownership(self):
        return (
            self.ownership_start_seconds if self.ownership_start_seconds is not None else self.start_seconds,
            self.ownership_end_seconds if self.ownership_end_seconds is not None else self.end_seconds,
        )
