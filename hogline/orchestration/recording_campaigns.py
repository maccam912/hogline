"""Durable date-range discovery → metadata → recording analysis campaigns."""

from datetime import date
from typing import Literal

from pydantic import Field, model_validator
from sqlalchemy import JSON, String, select
from sqlalchemy.orm import Mapped, mapped_column

from hogline.adapters.sources import digest, inventory_url, source_view
from hogline.db import (
    Base,
    DiscoveryFeed,
    DiscoveryMembership,
    Job,
    ProcessingBatch,
    Source,
    now,
)
from hogline.domain.models import Model, uid
from hogline.media.post_live import has_verified_download
from hogline.orchestration.discovery import FeedCreate, create_feed, enqueue_page
from hogline.orchestration.metadata import VERSION as METADATA_VERSION
from hogline.orchestration.metadata import source_pin
from hogline.orchestration.plans import BatchControl, control_batch, enqueue_in_session, publish_batch
from hogline.orchestration.recording import RecordingAnalysisRequest, recording_plan


class RecordingCampaign(Base):
    __tablename__ = "recording_campaign"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    state: Mapped[str] = mapped_column(String, default="active")
    document: Mapped[dict] = mapped_column(JSON)


class CampaignRequest(Model):
    start_date: date
    end_date: date
    sheets: list[int] = Field(default_factory=lambda: list(range(1, 9)), min_length=1, max_length=8)
    weekdays: list[int] = Field(default_factory=lambda: list(range(7)), min_length=1, max_length=7)
    feed_url: str | None = Field(default=None, max_length=500)
    title_filter: str = Field(default="", max_length=500)
    max_worker_seconds: int = Field(default=14400, ge=60, le=86400)
    max_image_growth_mb: int = Field(default=512, ge=32, le=4096)

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_weekday_scope(cls, value):
        if isinstance(value, dict) and "sundays_only" in value:
            value = dict(value)
            sunday = value.pop("sundays_only")
            if "weekdays" not in value and sunday:
                value["weekdays"] = [6]
        return value

    @model_validator(mode="after")
    def valid_scope(self):
        self.title_filter = self.title_filter.strip()
        if self.end_date < self.start_date:
            raise ValueError("End date must be on or after start date")
        if any(s not in range(1, 9) for s in self.sheets) or len(set(self.sheets)) != len(self.sheets):
            raise ValueError("Choose unique sheets from 1 to 8")
        if any(day not in range(7) for day in self.weekdays) or len(set(self.weekdays)) != len(self.weekdays):
            raise ValueError("Choose unique weekdays from Monday (0) to Sunday (6)")
        if self.feed_url:
            self.feed_url = inventory_url(self.feed_url)
        return self


class CampaignStart(Model):
    id: str = Field(min_length=1, max_length=100)
    request: CampaignRequest


class CampaignControl(Model):
    action: Literal["pause", "resume", "cancel"]


def title_matches(source, request):
    return request.title_filter.casefold() in (source.get("title") or "").casefold()


def matches(source, request, excluded):
    try:
        day = date.fromisoformat(source.get("title_date") or "")
    except ValueError:
        return False
    return (
        request.start_date <= day <= request.end_date
        and source.get("sheet") in request.sheets
        and day.weekday() in request.weekdays
        and title_matches(source, request)
        and not any(word.lower() in source.get("title", "").lower() for word in excluded)
    )


def candidates(session, request, excluded, feed_id=None, *, resolve_dates=False, tracked=()):
    member_ids = (
        None
        if not feed_id
        else set(
            session.scalars(
                select(DiscoveryMembership.source_id).where(DiscoveryMembership.feed_id == feed_id)
            )
        )
    )
    return [
        source_view(s.document)
        for s in session.scalars(select(Source))
        if (member_ids is None or s.id in member_ids)
        and (
            matches(source_view(s.document), request, excluded)
            or s.id in tracked
            or (
                resolve_dates
                and title_matches(s.document, request)
                and not s.document.get("title_date")
                and s.document.get("metadata_refresh_needed")
                and s.document.get("sheet") in request.sheets
                and not any(w.lower() in s.document.get("title", "").lower() for w in excluded)
            )
        )
    ]


def known_analyses(session):
    result = {}
    for identifier, state, purpose, request in session.execute(
        select(
            ProcessingBatch.id,
            ProcessingBatch.state,
            ProcessingBatch.document["campaign_purpose"],
            ProcessingBatch.document["request"],
        )
    ):
        if purpose == "recording_analysis" and state in ("active", "paused", "completed"):
            for source_id in request["source_ids"]:
                result[source_id] = {"id": identifier, "state": state}
    return result


def preview(db, request):
    with db.snapshot_session() as session:
        feed = (
            session.scalar(select(DiscoveryFeed).where(DiscoveryFeed.url == request.feed_url))
            if request.feed_url
            else None
        )
        sources = (
            []
            if request.feed_url and not feed
            else candidates(session, request, db.settings.auto_lineup_exclude, feed.id if feed else None)
        )
        known = known_analyses(session)
        items = [
            {
                "id": s["id"],
                "title": s["title"],
                "date": s.get("title_date"),
                "sheet": s.get("sheet"),
                "status": known[s["id"]]["state"] if s["id"] in known else "will_analyze",
                "hours": (s.get("duration_seconds") or 0) / 3600,
            }
            for s in sources
        ]
        return {
            "items": items,
            "hours": sum(s["hours"] for s in items),
            "discovery": bool(request.feed_url),
            "excluded_words": list(db.settings.auto_lineup_exclude),
            "feed_state": feed.document["backfill_state"] if feed else None,
        }


def start(db, body):
    # Feed registration is idempotent; it queues metadata listing, never analysis.
    feed = (
        create_feed(
            db, FeedCreate(url=body.request.feed_url, page_size=50, overlap=3, backfill_pages_per_cycle=0)
        )
        if body.request.feed_url
        else None
    )
    with db.snapshot_session(write=True) as session:
        row = session.get(RecordingCampaign, body.id)
        request = body.request.model_dump(mode="json")
        if row:
            if CampaignRequest.model_validate(row.document["request"]).model_dump(mode="json") != request:
                raise ValueError("Batch ID already belongs to different filters or limits")
            return view(row)
        row = RecordingCampaign(
            id=body.id,
            state="active",
            document={
                "request": request,
                "feed_id": feed["id"] if feed else None,
                "created_at": now(),
                "items": {},
                "discovery_state": "pending" if feed else "not_requested",
                "control_errors": [],
            },
        )
        session.add(row)
        session.flush()
        return view(row)


def view(row):
    items = list(row.document.get("items", {}).values())
    counts = {}
    for item in items:
        counts[item["state"]] = counts.get(item["state"], 0) + 1
    return {"id": row.id, "state": row.state, **row.document, "counts": counts}


def tick(db):
    """Bound each admission pass; all item/queue changes survive a browser or server restart."""
    with db.snapshot_session(write=True) as session:
        campaigns = list(
            session.scalars(select(RecordingCampaign).where(RecordingCampaign.state == "active"))
        )
        if not campaigns:
            return
        known = known_analyses(session)
        for row in campaigns:
            request = CampaignRequest.model_validate(row.document["request"])
            doc = dict(row.document)
            items = {k: dict(v) for k, v in doc["items"].items()}
            feed = session.get(DiscoveryFeed, doc["feed_id"]) if doc["feed_id"] else None
            if feed:
                doc["discovery_state"] = feed.document["backfill_state"]
                if doc["discovery_state"] == "ready":
                    enqueue_page(session, feed)
                elif doc["discovery_state"] not in ("visible_end_observed",):
                    doc["discovery_problem"] = feed.document.get("pause_reason") or doc["discovery_state"]
            sources = candidates(
                session,
                request,
                db.settings.auto_lineup_exclude,
                feed.id if feed else None,
                resolve_dates=bool(feed),
                tracked=items,
            )
            admitted = 0
            for source in sorted(
                sources, key=lambda s: (s.get("title_date") or "", s.get("sheet") or 0, s["id"])
            ):
                sid = source["id"]
                item = items.get(sid, {"source_id": sid, "title": source["title"], "state": "waiting"})
                items[sid] = item
                if item.get("batch_id"):
                    batch = session.get(ProcessingBatch, item["batch_id"])
                    item["state"] = batch.state if batch else "attention"
                    item["problem"] = batch.document.get("pause_reason") if batch else "Analysis was cleared"
                    continue
                if item["state"] == "attention":
                    continue
                if not title_matches(source, request):
                    item.update(state="excluded", problem="Video title does not match the selected filter")
                    continue
                if not source.get("metadata_refresh_needed") and not matches(
                    source, request, db.settings.auto_lineup_exclude
                ):
                    item.update(
                        state="excluded" if source.get("title_date") else "attention",
                        problem="Outside the selected dates or weekdays"
                        if source.get("title_date")
                        else "Recording date is unresolved; analysis was not queued",
                    )
                    continue
                if sid in known:
                    item.update(batch_id=known[sid]["id"], state=known[sid]["state"], owned=False)
                    continue
                if admitted >= 4:
                    continue
                admitted += 1
                if (
                    source.get("metadata_refresh_needed") and not has_verified_download(source)
                ) or not source.get("duration_seconds"):
                    job = session.get(Job, item["metadata_job_id"]) if item.get("metadata_job_id") else None
                    if job is None:
                        job = enqueue_in_session(
                            session,
                            {
                                "kind": "metadata_refresh",
                                "stage_version": METADATA_VERSION,
                                "source_hash": source["metadata_sha256"],
                                "source_pin": source_pin(source),
                                "request": {"source_id": sid, "timeout_seconds": 30},
                            },
                        )
                        item["metadata_job_id"] = job.id
                    item["state"] = "metadata" if job.state in ("queued", "running") else "attention"
                    if item["state"] == "attention":
                        item["problem"] = (
                            job.document.get("error")
                            or "Metadata still incomplete. Refresh it in Archive tools."
                        )
                    continue
                if source.get("live_status") in ("is_live", "is_upcoming") or (
                    source.get("live_status") == "post_live" and not has_verified_download(source)
                ):
                    item.update(
                        state="attention", problem="Stream is unfinished; try again after it is processed."
                    )
                    continue
                try:
                    plan = recording_plan(
                        session,
                        sid,
                        RecordingAnalysisRequest(
                            max_worker_seconds=request.max_worker_seconds,
                            max_image_growth_mb=request.max_image_growth_mb,
                        ),
                        db.settings.recording_parallelism,
                    )
                    plan["sha256"] = digest(
                        {k: v for k, v in plan.items() if k not in ("sha256", "created_at")}
                    )
                    batch = publish_batch(session, plan)
                    item.update(batch_id=batch["id"], state=batch["state"], owned=True)
                    known[sid] = {"id": batch["id"], "state": batch["state"]}
                except ValueError as exc:
                    item.update(state="attention", problem=str(exc))
            if feed and doc["discovery_state"] == "ready":
                doc.pop("discovery_problem", None)
            doc["items"] = items
            doc["updated_at"] = now()
            listing_done = not feed or doc["discovery_state"] == "visible_end_observed"
            pending = any(i["state"] in ("waiting", "metadata", "active", "paused") for i in items.values())
            if not listing_done and not pending and doc.get("discovery_problem"):
                row.state = "attention"
            if listing_done and not pending:
                row.state = (
                    "attention"
                    if any(i["state"] in ("attention", "failed", "cancelled") for i in items.values())
                    else "completed"
                )
            row.document = doc


def control(db, identifier, action):
    with db.snapshot_session(write=True) as session:
        row = session.get(RecordingCampaign, identifier)
        if row is None:
            raise ValueError("Batch not found")
        if row.state in ("completed", "cancelled"):
            raise ValueError("This batch has finished; create a new date-range batch")
        row.state = {"pause": "paused", "resume": "active", "cancel": "cancelled"}[action]
        own = [i["batch_id"] for i in row.document["items"].values() if i.get("owned") and i.get("batch_id")]
    errors = []
    for batch_id in own:
        with db.snapshot_session() as session:
            batch = session.get(ProcessingBatch, batch_id)
            eligible = batch and (
                batch.state == "active"
                if action == "pause"
                else batch.state == "paused"
                if action == "resume"
                else batch.state in ("active", "paused")
            )
        if eligible:
            try:
                control_batch(db, batch_id, BatchControl(action=action, retry_failed=action == "resume"))
            except ValueError as exc:
                errors.append(str(exc))
    with db.snapshot_session(write=True) as session:
        row = session.get(RecordingCampaign, identifier)
        items = {k: dict(v) for k, v in row.document["items"].items()}
        for item in items.values():
            if item.get("batch_id"):
                batch = session.get(ProcessingBatch, item["batch_id"])
                if batch:
                    item["state"] = batch.state
            elif action == "resume" and item["state"] == "attention":
                item["state"] = "waiting"
                item.pop("problem", None)
                # Metadata retries remain explicit in Jobs; successful manual retries
                # are picked up on the next admission pass.
        row.document = {**row.document, "items": items, "control_errors": errors}
        return view(row)
