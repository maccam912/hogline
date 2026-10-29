from contextlib import contextmanager
from datetime import UTC, datetime

from sqlalchemy import JSON, Float, ForeignKey, Index, Integer, String, UniqueConstraint, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from hogline.config import Settings
from hogline.domain.models import uid


def now():
    return datetime.now(UTC).isoformat()


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


class Database:
    def __init__(self, settings: Settings):
        self.settings = settings
        kwargs = (
            {"connect_args": {"check_same_thread": False, "timeout": 30}}
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
                session.connection().exec_driver_sql("BEGIN IMMEDIATE" if write else "BEGIN")
            elif self.engine.dialect.name == "postgresql":
                session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            else:
                raise ValueError("Snapshot consistency is configured for SQLite and PostgreSQL")
            yield session
