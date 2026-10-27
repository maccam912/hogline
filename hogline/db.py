from contextlib import contextmanager
from datetime import UTC, datetime

from sqlalchemy import JSON, ForeignKey, Integer, String, UniqueConstraint, create_engine, event
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


class Release(Base):
    __tablename__ = "dataset_release"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    created_at: Mapped[str] = mapped_column(String, default=now)
    sha256: Mapped[str] = mapped_column(String)
    document: Mapped[dict] = mapped_column(JSON)


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
