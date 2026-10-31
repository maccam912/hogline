"""Reviewable per-end anonymous positions and shot execution observations."""

from typing import Literal

from pydantic import Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.orm.exc import StaleDataError

from hogline.adapters.sources import digest
from hogline.db import GameRow, SkillMotion, SkillReview, SkillRevision, SkillSequence, Source
from hogline.domain.catalog import Conflict
from hogline.domain.models import Model
from hogline.domain.skill_aim import AimObservation
from hogline.domain.skill_calibration import CameraCalibration
from hogline.domain.skill_scope import compatible_game_scope
from hogline.orchestration.skill_calibration import (
    calibration_cohort,
    checked_calibration,
    checked_motion,
    checked_shot_aim,
)
from hogline.reconstruction.skill_candidates import delivery_candidates

Side = Literal["red", "yellow"]
Position = Literal["lead", "second", "third", "fourth"]


class Slot(Model):
    end: int = Field(ge=1, le=30)
    side: Side
    position: Position
    participant: str = Field(min_length=1, max_length=160)
    skip: bool = False
    acceptance: Literal["proposed", "confirmed"] = "proposed"
    basis: str = Field(default="operator", max_length=2000)


class Shot(Model):
    id: str = Field(min_length=1, max_length=160)
    source_id: str
    seconds: float = Field(ge=0)
    end: int | None = Field(default=None, ge=1, le=30)
    side: Literal["red", "yellow", "unknown"] = "unknown"
    team_delivery: int | None = Field(default=None, ge=1, le=8)
    participant: str | None = Field(default=None, min_length=1, max_length=160)
    acceptance: Literal["proposed", "confirmed", "excluded"] = "proposed"
    call: Literal["unknown", "draw", "guard", "freeze", "takeout", "peel", "raise"] = "unknown"
    turn: Literal["unknown", "left", "right"] = "unknown"
    direction: Literal["unknown", "near_to_far", "far_to_near"] = "unknown"
    # Target and measurement must share the same speed reference plane and calibration.
    speed_mps: float | None = Field(default=None, gt=0, le=20)
    target_speed_mps: float | None = Field(default=None, gt=0, le=20)
    calibration: str | None = Field(default=None, min_length=1, max_length=200)
    # Signed right-positive angle, looking from delivery hack toward target house.
    line_error_degrees: float | None = Field(default=None, ge=-90, le=90)
    line_calibration: str | None = Field(default=None, min_length=1, max_length=200)
    aim: AimObservation | None = None
    swept_seconds: float | None = Field(default=None, ge=0, le=120)
    evidence: list[str] = Field(default_factory=list, max_length=32)
    note: str = Field(default="", max_length=4000)

    @model_validator(mode="after")
    def evidence_required(self):
        if self.acceptance == "confirmed" and not self.evidence:
            raise ValueError("Confirmed shots need evidence references")
        if self.acceptance == "confirmed" and (self.end is None or self.side == "unknown"):
            raise ValueError("Confirm the shot's end and side before including it in reports")
        return self


class Participant(Model):
    id: str = Field(pattern=r"^skill-player:[A-Za-z0-9_-]+$", max_length=160)
    label: str = Field(min_length=1, max_length=120)
    actor_ids: list[str] = Field(min_length=1, max_length=100)
    acceptance: Literal["proposed", "confirmed", "excluded"] = "proposed"
    basis: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def distinct_actors(self):
        if len(set(self.actor_ids)) != len(self.actor_ids):
            raise ValueError("Participant actor references must be unique")
        if any(not a.startswith("skill-actor:") for a in self.actor_ids):
            raise ValueError("Participants link clip-local anonymous actor references")
        return self


class EndWindow(Model):
    """Reviewed time interval belonging to one standard four-player end."""

    id: str = Field(min_length=1, max_length=160)
    source_id: str
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(gt=0)
    end: int = Field(ge=1, le=30)
    first_side: Literal["red", "yellow", "unknown"] = "unknown"
    format: Literal["standard_four_player"] = "standard_four_player"
    acceptance: Literal["proposed", "confirmed", "excluded"] = "proposed"
    basis: str = Field(min_length=1, max_length=2000)
    evidence: list[str] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def ordered(self):
        if self.end_seconds <= self.start_seconds:
            raise ValueError("End interval must have positive duration")
        if self.acceptance == "confirmed" and not self.evidence:
            raise ValueError("Confirmed end intervals need evidence references")
        return self


def participant_aliases(document):
    return {
        actor: participant.id
        for participant in document.participants
        if participant.acceptance == "confirmed"
        for actor in participant.actor_ids
    }


def resolve_participant(document, identifier):
    return participant_aliases(document).get(identifier, identifier)


class SkillDocument(Model):
    slots: list[Slot] = Field(default_factory=list, max_length=240)
    shots: list[Shot] = Field(default_factory=list, max_length=1000)
    participants: list[Participant] = Field(default_factory=list, max_length=64)
    end_windows: list[EndWindow] = Field(default_factory=list, max_length=60)
    calibrations: list[CameraCalibration] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def unique(self):
        if len({c.id for c in self.calibrations}) != len(self.calibrations):
            raise ValueError("Calibration IDs must be unique")
        if len({w.id for w in self.end_windows}) != len(self.end_windows):
            raise ValueError("End interval IDs must be unique")
        windows = [w for w in self.end_windows if w.acceptance == "confirmed"]
        if len({(w.source_id, w.end) for w in windows}) != len(windows):
            raise ValueError("Use one confirmed interval per source and end")
        for i, a in enumerate(windows):
            for b in windows[i + 1 :]:
                if a.source_id == b.source_id and max(a.start_seconds, b.start_seconds) < min(
                    a.end_seconds, b.end_seconds
                ):
                    raise ValueError("Confirmed end intervals cannot overlap in a source")
        participant_ids = [p.id for p in self.participants]
        if len(set(participant_ids)) != len(participant_ids):
            raise ValueError("Game participant IDs must be unique")
        actors = [a for p in self.participants if p.acceptance == "confirmed" for a in p.actor_ids]
        if len(set(actors)) != len(actors):
            raise ValueError("An actor cannot belong to two confirmed participants")
        for reference in [s.participant for s in [*self.slots, *self.shots] if s.participant]:
            if reference.startswith("skill-player:") and reference not in participant_ids:
                raise ValueError("Shot or slot references an absent game participant")
        keys = [(s.end, s.side, s.position) for s in self.slots]
        if len(set(keys)) != len(keys):
            raise ValueError("Only one assignment per end, side and throwing position")
        people = [
            (s.end, resolve_participant(self, s.participant))
            for s in self.slots
            if s.acceptance == "confirmed"
        ]
        if len(set(people)) != len(people):
            raise ValueError("A confirmed participant cannot occupy two positions in the same end")
        skips = [(s.end, s.side) for s in self.slots if s.skip and s.acceptance == "confirmed"]
        if len(set(skips)) != len(skips):
            raise ValueError("Only one confirmed skip per side and end")
        ids = [s.id for s in self.shots]
        if len(set(ids)) != len(ids):
            raise ValueError("Shot IDs must be unique")
        numbered = [
            (s.end, s.side, s.team_delivery)
            for s in self.shots
            if s.acceptance == "confirmed" and s.team_delivery is not None
        ]
        if len(set(numbered)) != len(numbered):
            raise ValueError("Only one confirmed shot per end, side and team delivery number")
        moments = [(s.source_id, s.seconds) for s in self.shots if s.acceptance == "confirmed"]
        if len(set(moments)) != len(moments):
            raise ValueError("The same source moment cannot count as two confirmed shots")
        return self


class SkillEdit(Model):
    expected_revision: int = Field(ge=0)
    document: SkillDocument
    author: str = Field(default="Local operator", min_length=1, max_length=120)
    reason: str = Field(default="Skill review", min_length=1, max_length=2000)


def read_skills(session, game_id):
    game = session.get(GameRow, game_id)
    if game is None:
        raise ValueError("Game not found")
    row = session.get(SkillReview, game_id)
    if row and digest(row.document) != row.sha256:
        raise ValueError("Skill review checksum mismatch")
    stored = row.document if row else {}
    context = stored.get("_context")
    stale = bool(row) and (
        context is None or not compatible_game_scope(session, game, context["game_revision"])
    )
    if context:
        for source_id, expected in context["source_hashes"].items():
            source = session.get(Source, source_id)
            stale |= source is None or source.document["metadata_sha256"] != expected
        for sequence_id, expected in context.get("sequence_hashes", {}).items():
            sequence = session.get(SkillSequence, sequence_id)
            stale |= sequence is None or sequence.sha256 != expected or digest(sequence.document) != expected
        for motion_id, expected in context.get("motion_hashes", {}).items():
            motion = session.get(SkillMotion, motion_id)
            stale |= motion is None or motion.sha256 != expected or digest(motion.document) != expected
            if motion:
                try:
                    checked_motion(session, game, motion_id)
                except ValueError:
                    stale = True
    return {
        "game_id": game_id,
        "game_revision": game.revision,
        "spans": game.document["spans"],
        "revision": row.revision if row else 0,
        "document": {k: v for k, v in stored.items() if k != "_context"}
        if row
        else SkillDocument().model_dump(),
        "stale": stale,
    }


def save_skills(db, game_id, edit):
    with db.snapshot_session(write=True) as session:
        current = read_skills(session, game_id)
        if current["revision"] != edit.expected_revision:
            raise Conflict("Skill review changed; reload before saving")
        game = session.get(GameRow, game_id)
        if game.document.get("retired_into"):
            raise ValueError("Review the replacement game after a split or merge")
        referenced_sequences = {}

        def checked_sequence(sequence_id, source_id=None):
            sequence = session.get(SkillSequence, sequence_id)
            if sequence is None or digest(sequence.document) != sequence.sha256:
                raise ValueError("Sequence evidence is missing or changed")
            doc = sequence.document
            source = session.get(Source, doc["source_id"])
            if (
                sequence.game_id != game_id
                or not compatible_game_scope(session, game, doc["game_revision"])
                or (source_id is not None and doc["source_id"] != source_id)
                or source is None
                or doc["source_metadata_sha256"] != source.document["metadata_sha256"]
            ):
                raise ValueError("Sequence evidence no longer matches this game and source")
            referenced_sequences[sequence.id] = doc
            return doc

        def check_actor(participant, source_id=None, seconds=None):
            if not participant or not participant.startswith("skill-actor:"):
                return
            parts = participant.split(":")
            if len(parts) != 3:
                raise ValueError("Invalid anonymous actor reference")
            doc = checked_sequence(parts[1], source_id)
            if parts[2] not in {a["actor_tag"] for a in doc["proposal"].get("activities", [])}:
                raise ValueError("Anonymous actor is absent from sequence evidence")
            if (
                seconds is not None
                and not doc["request"]["start_seconds"] <= seconds <= doc["request"]["end_seconds"]
            ):
                raise ValueError(
                    "Anonymous actor is scoped to this sequence; review continuity before reusing it"
                )

        for participant in edit.document.participants:
            for actor in participant.actor_ids:
                check_actor(actor)
        for slot in edit.document.slots:
            check_actor(slot.participant)
        referenced_motions = {}
        calibration_checks = {}
        for calibration in edit.document.calibrations:
            checked = checked_calibration(
                session, game, calibration, db.settings.data_dir, edit.document.calibrations
            )
            if calibration.acceptance == "confirmed" and checked["fit"]["status"] != "validated_geometry":
                raise ValueError("Confirmed calibration must pass independent landmark validation")
            referenced_motions[calibration.motion_id] = calibration.motion_sha256
            calibration_checks[calibration.id] = (calibration, checked)
            anchor = checked["motion"].get("delivery_anchor")
            if anchor:
                checked_sequence(anchor["sequence_id"], calibration.source_id)
        for window in edit.document.end_windows:
            if not any(
                s["source_id"] == window.source_id
                and s["start_seconds"] <= window.start_seconds < window.end_seconds <= s["end_seconds"]
                for s in game.document["spans"]
            ):
                raise ValueError("End interval must lie within a game source span")
            for reference in window.evidence:
                if reference.startswith("skill-sequence:"):
                    parts = reference.split(":")
                    if len(parts) != 4 or parts[2] != "frame" or not parts[3].isdigit():
                        raise ValueError("Invalid end interval sequence reference")
                    doc = checked_sequence(parts[1], window.source_id)
                    if int(parts[3]) >= len(doc["samples"]):
                        raise ValueError("End interval evidence frame is absent")
        for shot in edit.document.shots:
            if shot.aim is not None:
                result = checked_shot_aim(shot, calibration_checks)
                if shot.aim.acceptance == "confirmed" and result["status"] != "experimental_line_measurement":
                    raise ValueError("Cannot confirm aim: " + result["reason"])
                shot.line_error_degrees = (
                    result["line_error_degrees"] if shot.aim.acceptance == "confirmed" else None
                )
                shot.line_calibration = calibration_cohort(calibration_checks[shot.aim.calibration_id][0])
            check_actor(shot.participant, shot.source_id, shot.seconds)
            if not any(
                s["source_id"] == shot.source_id and s["start_seconds"] <= shot.seconds <= s["end_seconds"]
                for s in game.document["spans"]
            ):
                raise ValueError("Shot must fall within a game source span")
            for reference in [*shot.evidence, *(shot.aim.evidence if shot.aim else [])]:
                if not reference.startswith("skill-sequence:"):
                    continue
                parts = reference.split(":")
                if len(parts) != 4 or parts[2] != "frame" or not parts[3].isdigit():
                    raise ValueError("Invalid sequence evidence reference")
                doc = checked_sequence(parts[1], shot.source_id)
                if int(parts[3]) >= len(doc["samples"]):
                    raise ValueError("Sequence evidence refers to an absent frame")
        confirmed = {shot.id for shot in edit.document.shots if shot.acceptance == "confirmed"}
        for candidate in delivery_candidates(referenced_sequences.values(), edit.document.shots):
            if len(set(candidate["linked_shot_ids"]) & confirmed) > 1:
                raise ValueError("Overlapping delivery observations must be resolved to one confirmed shot")
        document = {
            **edit.document.model_dump(),
            "_context": {
                "game_revision": game.revision,
                "source_hashes": {
                    s["source_id"]: session.get(Source, s["source_id"]).document["metadata_sha256"]
                    for s in game.document["spans"]
                },
                "sequence_hashes": {
                    identifier: digest(doc) for identifier, doc in referenced_sequences.items()
                },
                "motion_hashes": referenced_motions,
            },
        }
        row = session.get(SkillReview, game_id)
        if row is None:
            row = SkillReview(id=game_id, document=document, sha256=digest(document))
            session.add(row)
        else:
            row.document, row.sha256 = document, digest(document)
            flag_modified(row, "document")
        try:
            session.flush()
        except StaleDataError as exc:
            raise Conflict("Skill review changed; reload before saving") from exc
        session.add(
            SkillRevision(
                game_id=game_id,
                number=row.revision,
                document=document,
                sha256=row.sha256,
                author=edit.author,
                reason=edit.reason,
            )
        )
        return read_skills(session, game_id)


def role_proposals(document):
    """Standard four-player order; never infer skip from last delivery alone."""
    votes = {}
    for shot in document.shots:
        if (
            shot.acceptance == "excluded"
            or not shot.participant
            or shot.team_delivery is None
            or shot.end is None
            or shot.side == "unknown"
        ):
            continue
        position = ("lead", "second", "third", "fourth")[(shot.team_delivery - 1) // 2]
        participant = resolve_participant(document, shot.participant)
        votes.setdefault((shot.end, shot.side, position), {}).setdefault(participant, []).append(shot.id)
    proposals = []
    for (end, side, position), candidates in sorted(votes.items()):
        proposals.append(
            {
                "end": end,
                "side": side,
                "position": position,
                "status": "proposed" if len(candidates) == 1 else "conflict",
                "candidates": [{"participant": p, "shot_ids": ids} for p, ids in candidates.items()],
                "basis": "Standard four-player throwing order; team delivery indices require review",
            }
        )
    return proposals


def skill_history(session, game_id):
    read_skills(session, game_id)
    rows = session.scalars(
        select(SkillRevision).where(SkillRevision.game_id == game_id).order_by(SkillRevision.number)
    )
    result = []
    for row in rows:
        if digest(row.document) != row.sha256:
            raise ValueError("Skill history checksum mismatch")
        result.append(
            {
                "revision": row.number,
                "document": row.document,
                "author": row.author,
                "reason": row.reason,
                "created_at": row.created_at,
            }
        )
    return result
