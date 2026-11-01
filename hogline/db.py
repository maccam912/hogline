import sqlite3
import time
from contextlib import contextmanager
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    create_engine,
    event,
    literal,
    select,
)
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from sqlalchemy.schema import CreateIndex

from hogline.config import Settings
from hogline.domain.models import uid


def now():
    return datetime.now(UTC).isoformat()


def temporary_database_lock(exc):
    if not isinstance(exc, OperationalError):
        return False
    original = exc.orig
    code = getattr(original, "sqlite_errorcode", None)
    if code is not None:
        return code & 0xFF in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
    return str(original).lower() in ("database is locked", "database table is locked")


def rows_by_ids(session, model, column, identifiers, *, chunk_size=500):
    """Keep lookups below SQLite's bind limit without dropping or duplicating IDs."""
    values = sorted(set(identifiers))
    for offset in range(0, len(values), chunk_size):
        yield from session.scalars(select(model).where(column.in_(values[offset : offset + chunk_size])))


class Base(DeclarativeBase):
    pass


class Source(Base):
    __tablename__ = "source_video"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    source_key: Mapped[str] = mapped_column(String, unique=True)
    document: Mapped[dict] = mapped_column(JSON)


class MetadataSnapshot(Base):
    __tablename__ = "metadata_snapshot"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    source_id: Mapped[str] = mapped_column(ForeignKey("source_video.id"))
    sha256: Mapped[str] = mapped_column(String)
    document: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(String, default=now)
    __table_args__ = (UniqueConstraint("source_id", "sha256"),)


class Evidence(Base):
    __tablename__ = "evidence_frame"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    source_id: Mapped[str] = mapped_column(ForeignKey("source_video.id"))
    evidence_key: Mapped[str] = mapped_column(String, unique=True)
    document: Mapped[dict] = mapped_column(JSON)


class Job(Base):
    __tablename__ = "job"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    job_key: Mapped[str] = mapped_column(String, unique=True)
    state: Mapped[str] = mapped_column(String, default="queued")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    lease_until: Mapped[str | None] = mapped_column(String, nullable=True)
    document: Mapped[dict] = mapped_column(JSON)


class Scan(Base):
    __tablename__ = "activity_scan"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    job_id: Mapped[str] = mapped_column(ForeignKey("job.id"), unique=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("source_video.id"))
    document: Mapped[dict] = mapped_column(JSON)


class ProcessingBatch(Base):
    __tablename__ = "processing_batch"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    state: Mapped[str] = mapped_column(String, default="active")
    document: Mapped[dict] = mapped_column(JSON)
    __mapper_args__ = {"version_id_col": revision}


class AnalysisWindow(Base):
    __tablename__ = "analysis_window"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("job.id"), unique=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("source_video.id"))
    document: Mapped[dict] = mapped_column(JSON)


class ScoringSegment(Base):
    __tablename__ = "scoring_segment"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("job.id"), unique=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("source_video.id"))
    document: Mapped[dict] = mapped_column(JSON)


class JobExecution(Base):
    __tablename__ = "job_execution"
    id: Mapped[str] = mapped_column(String, primary_key=True)  # lease token
    job_id: Mapped[str] = mapped_column(ForeignKey("job.id"))
    batch_id: Mapped[str | None] = mapped_column(ForeignKey("processing_batch.id"), nullable=True)
    state: Mapped[str] = mapped_column(String)
    document: Mapped[dict] = mapped_column(JSON)


class DiscoveryFeed(Base):
    __tablename__ = "discovery_feed"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    url: Mapped[str] = mapped_column(String, unique=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    document: Mapped[dict] = mapped_column(JSON)
    __mapper_args__ = {"version_id_col": revision}


class DiscoveryRun(Base):
    __tablename__ = "discovery_run"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    request_key: Mapped[str] = mapped_column(String, unique=True)
    feed_id: Mapped[str] = mapped_column(ForeignKey("discovery_feed.id"))
    state: Mapped[str] = mapped_column(String, default="queued")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    lease_until: Mapped[str | None] = mapped_column(String, nullable=True)
    document: Mapped[dict] = mapped_column(JSON)


class DiscoveryMembership(Base):
    __tablename__ = "discovery_membership"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    feed_id: Mapped[str] = mapped_column(ForeignKey("discovery_feed.id"))
    source_id: Mapped[str] = mapped_column(ForeignKey("source_video.id"))
    document: Mapped[dict] = mapped_column(JSON)
    __table_args__ = (UniqueConstraint("feed_id", "source_id"),)


class FrameObservation(Base):
    __tablename__ = "frame_observation"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    evidence_id: Mapped[str] = mapped_column(ForeignKey("evidence_frame.id"))
    model_version: Mapped[str] = mapped_column(String)
    document: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(String, default=now)
    __table_args__ = (UniqueConstraint("evidence_id", "model_version"),)


class GameRow(Base):
    __tablename__ = "game"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    origin_key: Mapped[str | None] = mapped_column(String, nullable=True, unique=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    document: Mapped[dict] = mapped_column(JSON)
    __mapper_args__ = {"version_id_col": revision}


class SkillCampaign(Base):
    __tablename__ = "skill_campaign"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    game_id: Mapped[str] = mapped_column(ForeignKey("game.id"), index=True)
    plan_key: Mapped[str] = mapped_column(String, unique=True)
    state: Mapped[str] = mapped_column(String, default="active", index=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    document: Mapped[dict] = mapped_column(JSON)
    sha256: Mapped[str] = mapped_column(String)
    __mapper_args__ = {"version_id_col": revision}


class SkillDirectionRule(Base):
    __tablename__ = "skill_direction_rule"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    document: Mapped[dict] = mapped_column(JSON)
    sha256: Mapped[str] = mapped_column(String)
    __mapper_args__ = {"version_id_col": revision}


class SkillMotionBatch(Base):
    __tablename__ = "skill_motion_batch"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    game_id: Mapped[str] = mapped_column(ForeignKey("game.id"), index=True)
    plan_key: Mapped[str] = mapped_column(String, unique=True)
    state: Mapped[str] = mapped_column(String, default="active", index=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    document: Mapped[dict] = mapped_column(JSON)
    sha256: Mapped[str] = mapped_column(String)
    __mapper_args__ = {"version_id_col": revision}


class SkillSequence(Base):
    __tablename__ = "skill_sequence"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    game_id: Mapped[str] = mapped_column(ForeignKey("game.id"), index=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("job.id"), unique=True)
    sha256: Mapped[str] = mapped_column(String)
    document: Mapped[dict] = mapped_column(JSON)


class SkillMotion(Base):
    __tablename__ = "skill_motion"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    game_id: Mapped[str] = mapped_column(ForeignKey("game.id"), index=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("job.id"), unique=True)
    sha256: Mapped[str] = mapped_column(String)
    document: Mapped[dict] = mapped_column(JSON)


class SkillHouseContext(Base):
    __tablename__ = "skill_house_context"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    game_id: Mapped[str] = mapped_column(ForeignKey("game.id"), index=True)
    input_key: Mapped[str] = mapped_column(String, unique=True)
    created_at: Mapped[str] = mapped_column(String, default=now)
    document: Mapped[dict] = mapped_column(JSON)
    sha256: Mapped[str] = mapped_column(String)


class SkillReview(Base):
    __tablename__ = "skill_review"
    id: Mapped[str] = mapped_column(ForeignKey("game.id"), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    document: Mapped[dict] = mapped_column(JSON)
    sha256: Mapped[str] = mapped_column(String)
    __mapper_args__ = {"version_id_col": revision}


class SkillEstimateRevision(Base):
    """Append-only assistant/operator observations, separate from confirmed measurements."""

    __tablename__ = "skill_estimate_revision"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    game_id: Mapped[str] = mapped_column(ForeignKey("game.id"), index=True)
    number: Mapped[int] = mapped_column(Integer)
    document: Mapped[dict] = mapped_column(JSON)
    sha256: Mapped[str] = mapped_column(String)
    created_at: Mapped[str] = mapped_column(String, default=now)
    __table_args__ = (UniqueConstraint("game_id", "number"),)


class SkillDeliveryRay(Base):
    """Immutable local delivery-house measurement and receiving-image projection."""

    __tablename__ = "skill_delivery_ray"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    game_id: Mapped[str] = mapped_column(ForeignKey("game.id"), index=True)
    candidate_id: Mapped[str] = mapped_column(String, index=True)
    plan_key: Mapped[str] = mapped_column(String, unique=True)
    sha256: Mapped[str] = mapped_column(String)
    document: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(String, default=now)


class SkillRevision(Base):
    __tablename__ = "skill_revision"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    game_id: Mapped[str] = mapped_column(ForeignKey("game.id"))
    number: Mapped[int] = mapped_column(Integer)
    document: Mapped[dict] = mapped_column(JSON)
    sha256: Mapped[str] = mapped_column(String)
    author: Mapped[str] = mapped_column(String)
    reason: Mapped[str] = mapped_column(String)
    created_at: Mapped[str] = mapped_column(String, default=now)
    __table_args__ = (UniqueConstraint("game_id", "number"),)


class Revision(Base):
    __tablename__ = "interpretation_revision"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    game_id: Mapped[str] = mapped_column(ForeignKey("game.id"))
    number: Mapped[int] = mapped_column(Integer)
    document: Mapped[dict] = mapped_column(JSON)
    author: Mapped[str] = mapped_column(String)
    reason: Mapped[str] = mapped_column(String)
    created_at: Mapped[str] = mapped_column(String, default=now)
    __table_args__ = (UniqueConstraint("game_id", "number"),)


class ReviewTask(Base):
    __tablename__ = "review_task"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    task_key: Mapped[str] = mapped_column(String, unique=True)
    game_id: Mapped[str] = mapped_column(ForeignKey("game.id"))
    reason: Mapped[str] = mapped_column(String)
    state: Mapped[str] = mapped_column(String, default="open")


class Reconciliation(Base):
    __tablename__ = "game_reconciliation"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    state: Mapped[str] = mapped_column(String, default="active")
    document: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(String, default=now)
    __mapper_args__ = {"version_id_col": revision}


class Release(Base):
    __tablename__ = "dataset_release"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    created_at: Mapped[str] = mapped_column(String, default=now)
    sha256: Mapped[str] = mapped_column(String)
    document: Mapped[dict] = mapped_column(JSON)


class FrameLabels(Base):
    __tablename__ = "frame_labels"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    evidence_id: Mapped[str] = mapped_column(ForeignKey("evidence_frame.id"))
    view: Mapped[str] = mapped_column(String)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    document: Mapped[dict] = mapped_column(JSON)
    __mapper_args__ = {"version_id_col": revision}
    __table_args__ = (UniqueConstraint("evidence_id", "view"),)


class LabelRevision(Base):
    __tablename__ = "label_revision"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    labels_id: Mapped[str] = mapped_column(ForeignKey("frame_labels.id"))
    number: Mapped[int] = mapped_column(Integer)
    document: Mapped[dict] = mapped_column(JSON)
    author: Mapped[str] = mapped_column(String)
    reason: Mapped[str] = mapped_column(String)
    created_at: Mapped[str] = mapped_column(String, default=now)
    __table_args__ = (UniqueConstraint("labels_id", "number"),)


class EvaluationDataset(Base):
    __tablename__ = "evaluation_dataset"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    sha256: Mapped[str] = mapped_column(String)
    document: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(String, default=now)


class EvaluationRun(Base):
    __tablename__ = "evaluation_run"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    dataset_id: Mapped[str] = mapped_column(ForeignKey("evaluation_dataset.id"))
    model_version: Mapped[str] = mapped_column(String)
    sha256: Mapped[str] = mapped_column(String)
    document: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(String, default=now)


class BoardLabels(Base):
    __tablename__ = "board_labels"
    id: Mapped[str] = mapped_column(ForeignKey("evidence_frame.id"), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    document: Mapped[dict] = mapped_column(JSON)
    __mapper_args__ = {"version_id_col": revision}


class BoardLabelRevision(Base):
    __tablename__ = "board_label_revision"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    labels_id: Mapped[str] = mapped_column(ForeignKey("board_labels.id"))
    number: Mapped[int] = mapped_column(Integer)
    document: Mapped[dict] = mapped_column(JSON)
    author: Mapped[str] = mapped_column(String)
    reason: Mapped[str] = mapped_column(String)
    created_at: Mapped[str] = mapped_column(String, default=now)
    __table_args__ = (UniqueConstraint("labels_id", "number"),)


class BoardDataset(Base):
    __tablename__ = "board_evaluation_dataset"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    sha256: Mapped[str] = mapped_column(String)
    document: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(String, default=now)


class BoardEvaluation(Base):
    __tablename__ = "board_evaluation_run"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    dataset_id: Mapped[str] = mapped_column(ForeignKey("board_evaluation_dataset.id"))
    sha256: Mapped[str] = mapped_column(String)
    document: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(String, default=now)


class WindowTracks(Base):
    __tablename__ = "window_tracks"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    window_id: Mapped[str] = mapped_column(ForeignKey("analysis_window.id"))
    version: Mapped[str] = mapped_column(String)
    sha256: Mapped[str] = mapped_column(String)
    document: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(String, default=now)
    __table_args__ = (UniqueConstraint("window_id", "version"),)


class WindowHouseStates(Base):
    __tablename__ = "window_house_states"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    window_id: Mapped[str] = mapped_column(ForeignKey("analysis_window.id"))
    version: Mapped[str] = mapped_column(String)
    sha256: Mapped[str] = mapped_column(String)
    document: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(String, default=now)
    __table_args__ = (UniqueConstraint("window_id", "version"),)


class WindowActors(Base):
    __tablename__ = "window_actors"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    window_id: Mapped[str] = mapped_column(ForeignKey("analysis_window.id"))
    version: Mapped[str] = mapped_column(String)
    sha256: Mapped[str] = mapped_column(String)
    document: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(String, default=now)
    __table_args__ = (UniqueConstraint("window_id", "version"),)


class WindowDeliveries(Base):
    __tablename__ = "window_deliveries"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    window_id: Mapped[str] = mapped_column(ForeignKey("analysis_window.id"))
    actor_result_id: Mapped[str] = mapped_column(ForeignKey("window_actors.id"))
    version: Mapped[str] = mapped_column(String)
    sha256: Mapped[str] = mapped_column(String)
    document: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(String, default=now)
    __table_args__ = (UniqueConstraint("window_id", "version", "actor_result_id"),)


class PlayerGroup(Base):
    __tablename__ = "player_group"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    sha256: Mapped[str] = mapped_column(String)
    document: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(String, default=now)
    __mapper_args__ = {"version_id_col": revision}


class ActorIdentity(Base):
    __tablename__ = "actor_identity"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    actor_result_id: Mapped[str] = mapped_column(ForeignKey("window_actors.id"))
    track_id: Mapped[str] = mapped_column(String)
    source_id: Mapped[str] = mapped_column(ForeignKey("source_video.id"))
    view: Mapped[str] = mapped_column(String)
    start_seconds: Mapped[float] = mapped_column(Float)
    end_seconds: Mapped[float] = mapped_column(Float)
    group_id: Mapped[str] = mapped_column(ForeignKey("player_group.id"))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    sha256: Mapped[str] = mapped_column(String)
    document: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(String, default=now)
    __mapper_args__ = {"version_id_col": revision}
    __table_args__ = (
        UniqueConstraint("actor_result_id", "track_id"),
        Index("actor_identity_overlap", "source_id", "view", "start_seconds", "end_seconds"),
    )


class PlayerRevision(Base):
    __tablename__ = "player_revision"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    kind: Mapped[str] = mapped_column(String)
    entity_id: Mapped[str] = mapped_column(String)
    number: Mapped[int] = mapped_column(Integer)
    author: Mapped[str] = mapped_column(String)
    reason: Mapped[str] = mapped_column(String)
    sha256: Mapped[str] = mapped_column(String)
    document: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(String, default=now)
    __table_args__ = (UniqueConstraint("kind", "entity_id", "number"),)


class HandleModel(Base):
    __tablename__ = "handle_model"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    version: Mapped[str] = mapped_column(String, unique=True)
    sha256: Mapped[str] = mapped_column(String)
    document: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(String, default=now)


class HandleTrainingRun(Base):
    __tablename__ = "handle_training_run"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    dataset_id: Mapped[str] = mapped_column(ForeignKey("evaluation_dataset.id"))
    model_id: Mapped[str] = mapped_column(ForeignKey("handle_model.id"))
    sha256: Mapped[str] = mapped_column(String)
    document: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(String, default=now)


class HouseLabels(Base):
    __tablename__ = "house_labels"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    window_id: Mapped[str] = mapped_column(ForeignKey("analysis_window.id"))
    view: Mapped[str] = mapped_column(String)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    document: Mapped[dict] = mapped_column(JSON)
    __mapper_args__ = {"version_id_col": revision}
    __table_args__ = (UniqueConstraint("window_id", "view"),)


class HouseLabelRevision(Base):
    __tablename__ = "house_label_revision"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    labels_id: Mapped[str] = mapped_column(ForeignKey("house_labels.id"))
    number: Mapped[int] = mapped_column(Integer)
    document: Mapped[dict] = mapped_column(JSON)
    author: Mapped[str] = mapped_column(String)
    reason: Mapped[str] = mapped_column(String)
    created_at: Mapped[str] = mapped_column(String, default=now)
    __table_args__ = (UniqueConstraint("labels_id", "number"),)


class HouseDataset(Base):
    __tablename__ = "house_evaluation_dataset"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    sha256: Mapped[str] = mapped_column(String)
    document: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(String, default=now)


class HouseEvaluation(Base):
    __tablename__ = "house_evaluation_run"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    dataset_id: Mapped[str] = mapped_column(ForeignKey("house_evaluation_dataset.id"))
    sha256: Mapped[str] = mapped_column(String)
    document: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(String, default=now)


class ScoreConflictReview(Base):
    __tablename__ = "score_conflict_review"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    conflict_id: Mapped[str] = mapped_column(String)
    revision: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String)
    document: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(String, default=now)
    __table_args__ = (UniqueConstraint("conflict_id", "revision"),)


class LineupSegment(Base):
    """Face observations for one bounded game interval; templates live in the biometric store."""

    __tablename__ = "lineup_segment"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("job.id"), unique=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("source_video.id"))
    sha256: Mapped[str] = mapped_column(String)
    document: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(String, default=now)


class GameLineup(Base):
    __tablename__ = "game_lineup"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("source_video.id"))
    batch_id: Mapped[str] = mapped_column(String, index=True)
    sha256: Mapped[str] = mapped_column(String)
    document: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(String, default=now)


class LineupPerson(Base):
    __tablename__ = "lineup_person"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    sha256: Mapped[str] = mapped_column(String)
    document: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(String, default=now)
    __mapper_args__ = {"version_id_col": revision}


class LineupLink(Base):
    """An operator's decision that one anonymous game player is a named person."""

    __tablename__ = "lineup_link"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    lineup_id: Mapped[str] = mapped_column(ForeignKey("game_lineup.id"), index=True)
    person_id: Mapped[str] = mapped_column(ForeignKey("lineup_person.id"), index=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    sha256: Mapped[str] = mapped_column(String)
    document: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(String, default=now)
    __mapper_args__ = {"version_id_col": revision}


class LineupRevision(Base):
    __tablename__ = "lineup_revision"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    kind: Mapped[str] = mapped_column(String)
    entity_id: Mapped[str] = mapped_column(String)
    number: Mapped[int] = mapped_column(Integer)
    author: Mapped[str] = mapped_column(String)
    reason: Mapped[str] = mapped_column(String)
    sha256: Mapped[str] = mapped_column(String)
    document: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(String, default=now)
    __table_args__ = (UniqueConstraint("kind", "entity_id", "number"),)


class LineupGrouping(Base):
    """An undoable operator decision that several face groups are one person."""

    __tablename__ = "lineup_grouping"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    sha256: Mapped[str] = mapped_column(String)
    document: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(String, default=now)


class LineupSeparation(Base):
    """An operator's decision that a game player is not another player or a named person.

    Cross-game face identities are recomputed from templates; separations keep a rejected
    pairing apart on every recomputation.
    """

    __tablename__ = "lineup_separation"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    player_id: Mapped[str] = mapped_column(String, index=True)
    sha256: Mapped[str] = mapped_column(String)
    document: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(String, default=now)


class AutoLineup(Base):
    """One recording's place in the automatic face-finding queue (one row per recording)."""

    __tablename__ = "auto_lineup"
    source_id: Mapped[str] = mapped_column(ForeignKey("source_video.id"), primary_key=True)
    state: Mapped[str] = mapped_column(String, index=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    document: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(String, default=now)
    __mapper_args__ = {"version_id_col": revision}


def queue_field(model, key):
    """Fixed JSON queue keys must render literally for SQLite expression indexes to match."""
    return model.document[literal(key, type_=JSON.JSONStrIndexType(), literal_execute=True)].as_string()


QUEUE_INDEXES = [
    Index("evidence_source", Evidence.source_id, Evidence.id),
    Index("scan_source", Scan.source_id),
    Index("window_source", AnalysisWindow.source_id, AnalysisWindow.id),
    Index("execution_batch", JobExecution.batch_id),
    Index("job_state_owner", Job.state, queue_field(Job, "batch_id"), Job.attempts),
    Index("job_state_lease", Job.state, Job.lease_until),
    Index(
        "recording_admission",
        queue_field(ProcessingBatch, "campaign_purpose"),
        ProcessingBatch.state,
        queue_field(ProcessingBatch, "created_at"),
        ProcessingBatch.id,
    ),
    Index(
        "recording_cleanup",
        ProcessingBatch.state,
        queue_field(ProcessingBatch, "storage_policy"),
        queue_field(ProcessingBatch, "image_cleanup"),
    ),
]


class Database:
    def __init__(self, settings: Settings):
        self.settings = settings
        kwargs = (
            {"connect_args": {"check_same_thread": False, "timeout": 5}}
            if settings.database_url.startswith("sqlite")
            else {}
        )
        self.engine = create_engine(settings.database_url, **kwargs)
        if self.engine.dialect.name == "sqlite":

            @event.listens_for(self.engine, "connect")
            def pragmas(conn, _):
                conn.execute("PRAGMA foreign_keys=ON")
                conn.execute("PRAGMA journal_mode=WAL")

        Base.metadata.create_all(self.engine)
        # create_all does not add new indexes to existing tables.
        with self.engine.begin() as connection:
            for index in QUEUE_INDEXES:
                connection.execute(CreateIndex(index, if_not_exists=True))
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)

    @contextmanager
    def session(self):
        with self.sessions.begin() as session:
            yield session

    @contextmanager
    def snapshot_session(self, *, write=False):
        """One database view across all reads; reserve SQLite writes before reading."""
        with self.sessions.begin() as session:
            if self.engine.dialect.name == "sqlite":
                # Python's legacy SQLite mode does not begin a transaction for
                # SELECT. Explicit BEGIN is necessary for a real read snapshot.
                deadline = time.monotonic() + 60
                while True:
                    try:
                        session.connection().exec_driver_sql("BEGIN IMMEDIATE" if write else "BEGIN")
                        break
                    except OperationalError as exc:
                        if not write or not temporary_database_lock(exc) or time.monotonic() >= deadline:
                            raise
                        time.sleep(0.1)
            elif self.engine.dialect.name == "postgresql":
                session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            else:
                raise ValueError("Snapshot consistency is configured for SQLite and PostgreSQL")
            yield session
