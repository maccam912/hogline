"""Independent visual end reviews, retaining source evidence and earlier decisions."""

from typing import Literal
from uuid import NAMESPACE_URL, uuid5

from pydantic import Field, model_validator
from sqlalchemy import select

from hogline.adapters.sources import digest
from hogline.analytics.scorecards import scorecard_observations_in_session, select_inputs
from hogline.db import Evidence, FrameObservation, now
from hogline.domain.catalog import source_or_error
from hogline.domain.models import Model
from hogline.domain.provenance import observation_manifest, resolve_observation_manifests
from hogline.orchestration.scorecards import checked_image
from hogline.perception.broadcast import broadcast_catalog_in_session
from hogline.reconstruction.scorecards import reconstruct_scorecards

VERSION = "independent-visual-ice-end-v1"


class ConcessionReview(Model):
    rule: Literal["world-curling-2025-R12h"]
    conceding_color: Literal["red", "yellow"]
    red_remaining: int | None = Field(default=None, ge=0, le=8)
    yellow_remaining: int | None = Field(default=None, ge=0, le=8)


class ScoreOption(Model):
    red: int = Field(ge=0, le=8)
    yellow: int = Field(ge=0, le=8)

    @model_validator(mode="after")
    def one_scoring_color(self):
        if self.red and self.yellow:
            raise ValueError("A score option can award points to only one color")
        return self


class ScorePolicy(Model):
    kind: Literal["lower_bound", "recording_cutoff_blank"]
    alternatives: list[ScoreOption] = Field(default_factory=list, max_length=9)


class IceEndReview(Model):
    source_id: str
    game_key: str = Field(min_length=1, max_length=120)
    board_history_id: str | None = None
    observed_end_index: int = Field(ge=1, le=32)
    official_end_number: int | None = Field(default=None, ge=1, le=32)
    view: Literal["house_top", "house_bottom"]
    red_points: int | None = Field(default=None, ge=0, le=8)
    yellow_points: int | None = Field(default=None, ge=0, le=8)
    phase: Literal["unresolved", "end_finished", "game_finished"] = "unresolved"
    end_completed: bool | None = None
    concession: ConcessionReview | None = None
    score_policy: ScorePolicy | None = None
    final_state_evidence_ids: list[str] = Field(min_length=2, max_length=12)
    phase_evidence_ids: list[str] = Field(min_length=2, max_length=32)
    author: str = Field(min_length=1, max_length=120)
    reviewer_kind: Literal["assistant_visual", "human"]
    note: str = Field(min_length=1, max_length=4000)
    supersedes_review_ids: list[str] = Field(default_factory=list, max_length=12)

    @model_validator(mode="after")
    def valid_score(self):
        r, y = self.red_points, self.yellow_points
        if (r is None) != (y is None) or (r is not None and r > 0 and y > 0):
            raise ValueError("Ice scores need both values and at most one scoring color")
        if len(set(self.final_state_evidence_ids)) < 2 or len(set(self.phase_evidence_ids)) < 2:
            raise ValueError("An independent end review needs distinct final-state and phase frames")
        if self.concession:
            remaining = [self.concession.red_remaining, self.concession.yellow_remaining]
            if self.phase != "game_finished" or self.end_completed is True:
                raise ValueError(
                    "Concession facts require a game-ending review without completed-end assertion"
                )
            if all(v == 0 for v in remaining):
                raise ValueError("All stones played requires a completed-end review")
        if self.score_policy:
            if self.concession:
                raise ValueError("A score policy cannot replace concession rules")
            if self.score_policy.kind == "lower_bound":
                options = {(o.red, o.yellow) for o in self.score_policy.alternatives}
                if (
                    len(options) < 2
                    or len(options) != len(self.score_policy.alternatives)
                    or (r, y) not in options
                    or not all(r <= a and y <= b for a, b in options)
                    or self.end_completed is not True
                    or self.phase == "unresolved"
                ):
                    raise ValueError(
                        "Lower-bound policy needs distinct alternatives, their minimum, and a completed end"
                    )
            elif (
                self.score_policy.alternatives
                or (r, y) != (0, 0)
                or self.end_completed is True
                or self.phase != "unresolved"
            ):
                raise ValueError("Recording cutoff must be a policy blank without physical completion")
        return self


def check_review(document, frames):
    if digest({k: v for k, v in document.items() if k != "sha256"}) != document.get("sha256"):
        raise ValueError("Independent ice review checksum mismatch")
    request = IceEndReview.model_validate(document["request"])
    if document["board_history"] and digest(document["board_history"]) != document["board_history_sha256"]:
        raise ValueError("Independent ice review board basis checksum mismatch")
    expected = set(request.final_state_evidence_ids + request.phase_evidence_ids)
    if set(document["evidence_refs"]) != expected:
        raise ValueError("Independent ice review evidence list mismatch")
    for identifier, pin in document["evidence_refs"].items():
        frame = frames.get(identifier)
        if (
            frame is None
            or frame["source_id"] != request.source_id
            or digest(frame) != pin["document_sha256"]
        ):
            raise ValueError("Independent ice review evidence changed or missing")
    times = [frames[i]["source_seconds"] for i in request.final_state_evidence_ids]
    if max(times) - min(times) < 1:
        raise ValueError("Final-state evidence must span at least one second")


def save_ice_end_review(db, request):
    request = IceEndReview.model_validate(request)
    with db.snapshot_session(write=True) as session:
        source = source_or_error(session, request.source_id)
        frame_ids = set(request.final_state_evidence_ids + request.phase_evidence_ids)
        frames = {
            e.id: e.document for e in session.scalars(select(Evidence).where(Evidence.id.in_(frame_ids)))
        }
        if len(frames) != len(frame_ids):
            raise ValueError("Original evidence is missing")
        resolved = resolve_observation_manifests(
            session, [{"id": i, "evidence_id": i, "source_id": request.source_id} for i in frame_ids]
        )
        if any(observation_manifest(o) != source["metadata_sha256"] for o in resolved):
            raise ValueError("Review evidence has a different or unresolved source manifest")
        for f in frames.values():
            if f.get("acquisition_tier") == "coarse":
                raise ValueError("Review final scores using retained native frames")
            checked_image(db.settings.data_dir, f["image"])
        history = None
        if request.board_history_id:
            obs = scorecard_observations_in_session(session, request.source_id)
            old = (
                broadcast_catalog_in_session(session, db.settings.data_dir)["observations"] if not obs else []
            )
            report = reconstruct_scorecards(request.source_id, select_inputs(request.source_id, old, obs))
            history = next((h for h in report["scorecards"] if h["id"] == request.board_history_id), None)
            if history is None:
                raise ValueError("Board history changed or not found; inspect the current history")
        for identifier in request.supersedes_review_ids:
            previous = session.get(FrameObservation, identifier)
            if previous is None or previous.document.get("kind") != VERSION:
                raise ValueError("Superseded review not found")
            old = previous.document["request"]
            if (old["source_id"], old["game_key"], old["observed_end_index"]) != (
                request.source_id,
                request.game_key,
                request.observed_end_index,
            ):
                raise ValueError("A correction must refer to the same source, game and observed end")
            if old["reviewer_kind"] == "human" and request.reviewer_kind != "human":
                raise ValueError("Assistant review cannot replace a human decision")
        serialized_request = request.model_dump(
            exclude={name for name in ("concession", "score_policy") if getattr(request, name) is None}
        )
        body = {
            "kind": VERSION,
            "model_version": f"{VERSION}:{digest(serialized_request)}",
            "request": serialized_request,
            "source_metadata_sha256": source["metadata_sha256"],
            "board_history": history,
            "board_history_sha256": digest(history) if history else None,
            "evidence_refs": {
                i: {
                    "document_sha256": digest(f),
                    "source_image": f["image"],
                    "source_seconds": f["source_seconds"],
                    "pts": f["pts"],
                    "time_base_num": f["time_base_num"],
                    "time_base_den": f["time_base_den"],
                }
                for i, f in frames.items()
            },
            "acceptance": "proposed" if request.reviewer_kind == "assistant_visual" else "human_confirmed",
            "evidence_id": request.final_state_evidence_ids[0],
            "source_id": request.source_id,
        }
        identifier = str(uuid5(NAMESPACE_URL, digest(body)))
        existing = session.get(FrameObservation, identifier)
        if existing is None:
            legacy = {**body, "model_version": VERSION}
            existing = session.get(FrameObservation, str(uuid5(NAMESPACE_URL, digest(legacy))))
        if existing:
            return existing.document
        document = {**body, "id": identifier, "created_at": now()}
        document["sha256"] = digest(document)
        check_review(document, frames)
        session.add(
            FrameObservation(
                id=identifier,
                evidence_id=document["evidence_id"],
                model_version=document["model_version"],
                document=document,
            )
        )
        return document


def ice_reviews_in_session(session, source_id=None):
    rows = list(
        session.scalars(select(FrameObservation).where(FrameObservation.model_version.startswith(VERSION)))
    )
    documents = [r.document for r in rows if source_id is None or r.document["source_id"] == source_id]
    ids = {i for d in documents for i in d["evidence_refs"]}
    frames = {e.id: e.document for e in session.scalars(select(Evidence).where(Evidence.id.in_(ids)))}
    for d in documents:
        check_review(d, frames)
    return documents


def source_score_history(db, source_id):
    from hogline.reconstruction.scoring import reconcile_scores

    with db.snapshot_session() as session:
        source_or_error(session, source_id)
        inspected = scorecard_observations_in_session(session, source_id)
        old = (
            broadcast_catalog_in_session(session, db.settings.data_dir)["observations"]
            if not inspected
            else []
        )
        report = reconstruct_scorecards(source_id, select_inputs(source_id, old, inspected))
        return reconcile_scores(report, ice_reviews_in_session(session, source_id))
