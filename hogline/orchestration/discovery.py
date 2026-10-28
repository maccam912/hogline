"""Incremental metadata discovery. No media work is implied by finding a video."""

from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import Field, model_validator
from sqlalchemy import select, update
from sqlalchemy.orm.exc import StaleDataError

from hogline.adapters.sources import digest, import_youtube, inventory_url, listing_page
from hogline.db import DiscoveryFeed, DiscoveryMembership, DiscoveryRun, Source, now
from hogline.domain.models import Model, uid


def later(seconds):
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat()


class FeedCreate(Model):
    url: str = Field(max_length=500)
    page_size: int = Field(default=12, ge=2, le=50)
    overlap: int = Field(default=3, ge=1, le=20)
    reconcile_hours: int = Field(default=24, ge=1, le=720)
    scheduled: bool = False
    backfill_pages_per_cycle: int = Field(default=2, ge=0, le=20)

    @model_validator(mode="after")
    def valid(self):
        self.url = inventory_url(self.url)
        if self.overlap >= self.page_size:
            raise ValueError("Page overlap must be smaller than the page size")
        return self


def enqueue_page(session, feed, mode="advance", slot=None):
    doc = feed.document
    request = {
        "url": feed.url,
        "mode": mode,
        "generation": doc["generation"],
        "start": doc["next_index"] if mode == "advance" else 1,
        "limit": doc["config"]["page_size"],
        "anchors": doc["anchors"] if mode == "advance" else [],
        "slot": slot,
    }
    key = digest({"feed_id": feed.id, **request})
    row = session.scalar(select(DiscoveryRun).where(DiscoveryRun.request_key == key))
    if row is None:
        row = DiscoveryRun(
            feed_id=feed.id,
            request_key=key,
            document={"request": request, "created_at": now(), "error": None},
        )
        session.add(row)
        session.flush()
    return row


def create_feed(db, request):
    with db.session() as session:
        existing = session.scalar(select(DiscoveryFeed).where(DiscoveryFeed.url == request.url))
        if existing:
            return feed_view(session, existing)
        feed = DiscoveryFeed(
            url=request.url,
            document={
                "config": request.model_dump(),
                "created_at": now(),
                "generation": 1,
                "next_index": 1,
                "anchors": [],
                "backfill_state": "ready",
                "cycle_remaining": max(1, request.backfill_pages_per_cycle),
                "next_head_at": later(request.reconcile_hours * 3600),
                "pause_reason": None,
            },
        )
        session.add(feed)
        session.flush()
        enqueue_page(session, feed)
        return feed_view(session, feed)


class FeedControl(Model):
    action: Literal["next", "head", "restart", "pause", "resume", "configure"]
    scheduled: bool | None = None


def control_feed(db, feed_id, control):
    with db.session() as session:
        feed = session.get(DiscoveryFeed, feed_id)
        if feed is None:
            raise ValueError("Discovery feed not found")
        doc = feed.document
        if control.scheduled is not None:
            doc = {**doc, "config": {**doc["config"], "scheduled": control.scheduled}}
        if control.action == "pause":
            doc = (
                doc
                if doc["backfill_state"] == "paused"
                else {
                    **doc,
                    "paused_from": doc["backfill_state"],
                    "backfill_state": "paused",
                    "cycle_remaining": 0,
                    "pause_reason": "operator",
                }
            )
        elif control.action == "resume":
            if doc["backfill_state"] in ("needs_reconcile", "needs_attention"):
                raise ValueError("Inspect the failure and restart this pass; its old cursor is not trusted")
            doc = {**doc, "backfill_state": doc.get("paused_from", "ready"), "pause_reason": None}
        elif control.action == "restart":
            for old_run in session.scalars(
                select(DiscoveryRun).where(
                    DiscoveryRun.feed_id == feed.id, DiscoveryRun.state.in_(["queued", "failed"])
                )
            ):
                old_run.state = "cancelled"
            doc = {
                **doc,
                "generation": doc["generation"] + 1,
                "next_index": 1,
                "anchors": [],
                "last_page_ids": [],
                "backfill_state": "ready",
                "cycle_remaining": 0,
                "pause_reason": None,
            }
        elif control.action == "next" and doc["backfill_state"] != "ready":
            raise ValueError("Resume or restart this discovery pass before advancing")
        feed.document = doc
        if control.action in ("next", "restart", "head"):
            enqueue_page(
                session,
                feed,
                "head" if control.action == "head" else "advance",
                uid() if control.action == "head" else None,
            )
        session.flush()
        return feed_view(session, feed)


def feed_view(session, feed):
    members = list(session.scalars(select(DiscoveryMembership).where(DiscoveryMembership.feed_id == feed.id)))
    runs = list(session.scalars(select(DiscoveryRun).where(DiscoveryRun.feed_id == feed.id)))
    runs.sort(key=lambda r: r.document["created_at"], reverse=True)
    return {
        "id": feed.id,
        "url": feed.url,
        "revision": feed.revision,
        **feed.document,
        "discovered_sources": len(members),
        "runs": [
            {
                "id": r.id,
                "state": r.state,
                "attempts": r.attempts,
                "lease_until": r.lease_until,
                **{k: v for k, v in r.document.items() if k != "response"},
            }
            for r in runs[:20]
        ],
    }


def feeds(db):
    with db.session() as session:
        return [feed_view(session, f) for f in session.scalars(select(DiscoveryFeed))]


def schedule_due(db):
    with db.session() as session:
        for feed in session.scalars(select(DiscoveryFeed)):
            doc = feed.document
            if doc["backfill_state"] == "paused":
                continue
            if doc["config"]["scheduled"] and doc["next_head_at"] <= now():
                if doc["backfill_state"] == "visible_end_observed":
                    doc = {
                        **doc,
                        "generation": doc["generation"] + 1,
                        "next_index": 1,
                        "anchors": [],
                        "last_page_ids": [],
                        "backfill_state": "ready",
                    }
                    feed.document = doc
                    enqueue_page(session, feed)
                else:
                    enqueue_page(session, feed, "head", doc["next_head_at"])
                feed.document = {
                    **doc,
                    "next_head_at": later(doc["config"]["reconcile_hours"] * 3600),
                    "cycle_remaining": doc["config"]["backfill_pages_per_cycle"],
                }
            pending = session.scalar(
                select(DiscoveryRun).where(
                    DiscoveryRun.feed_id == feed.id,
                    DiscoveryRun.state.in_(["queued", "running", "failed"]),
                    DiscoveryRun.document["request"]["generation"].as_integer()
                    == feed.document["generation"],
                )
            )
            doc = feed.document
            if not pending and doc["backfill_state"] == "ready" and doc["cycle_remaining"] > 0:
                enqueue_page(session, feed)


def claim_discovery(db):
    with db.session() as session:
        for expired in session.scalars(
            select(DiscoveryRun).where(DiscoveryRun.state == "running", DiscoveryRun.lease_until < now())
        ):
            expired.state, expired.lease_until = "failed", None
            expired.document = {
                **expired.document,
                "error": "Discovery work lease expired",
                "retry_at": now(),
            }
            if expired.attempts >= 3:
                feed = session.get(DiscoveryFeed, expired.feed_id)
                feed.document = {
                    **feed.document,
                    "backfill_state": "needs_attention",
                    "cycle_remaining": 0,
                    "pause_reason": "Discovery lease expired after 3 attempts",
                }
        candidates = list(
            session.scalars(
                select(DiscoveryRun).where(
                    DiscoveryRun.state.in_(["queued", "failed"]), DiscoveryRun.attempts < 3
                )
            )
        )
        candidates.sort(key=lambda r: r.document["created_at"])
        for run in candidates:
            if run.document.get("retry_at", "") > now():
                continue
            feed = session.get(DiscoveryFeed, run.feed_id)
            if feed.document["backfill_state"] == "paused":
                continue
            other = session.scalar(
                select(DiscoveryRun).where(DiscoveryRun.feed_id == feed.id, DiscoveryRun.state == "running")
            )
            if other:
                continue
            token = uid()
            result = session.execute(
                update(DiscoveryRun)
                .where(
                    DiscoveryRun.id == run.id,
                    DiscoveryRun.state.in_(["queued", "failed"]),
                    DiscoveryRun.attempts == run.attempts,
                )
                .values(
                    state="running",
                    attempts=run.attempts + 1,
                    lease_until=later(240),
                    document={**run.document, "lease_token": token, "error": None},
                )
            )
            if result.rowcount == 1:
                session.refresh(run)
                # Optimistic feed revision serializes claims for this feed.
                feed.document = {**feed.document, "last_claim_at": now()}
                return {"id": run.id, "feed_id": feed.id, **run.document}


class DiscoveryPaused(ValueError):
    pass


def owned(session, run, allow_paused=False):
    row = session.get(DiscoveryRun, run["id"])
    if row.state != "running" or row.document.get("lease_token") != run["lease_token"]:
        raise ValueError("Discovery lease was reclaimed; this worker cannot publish")
    if not allow_paused and session.get(DiscoveryFeed, row.feed_id).document["backfill_state"] == "paused":
        raise DiscoveryPaused("Discovery paused; acquired metadata is retained for resume")
    return row


def execute_discovery(db, run):
    with db.session() as session:
        owned(session, run)
    request = run["request"]
    response = run.get("response")
    if response is None:
        response = listing_page(request["url"], request["start"], request["limit"])
        with db.session() as session:
            row = owned(session, run, allow_paused=True)
            row.document = {**row.document, "response": response, "acquired_at": now()}
            row.lease_until = later(240)
    sources = []
    for entry in response["entries"]:
        with db.session() as session:
            owned(session, run)
        metadata = entry["metadata"]
        sources.append(
            import_youtube(
                db, f"https://www.youtube.com/watch?v={metadata['id']}", metadata, metadata_kind="listing"
            )
        )
    identifiers = [e["metadata"]["id"] for e in response["entries"]]
    continuity = not request["anchors"] or bool(set(request["anchors"]) & set(identifiers))
    with db.session() as session:
        row = owned(session, run)
        feed = session.get(DiscoveryFeed, run["feed_id"])
        doc = feed.document
        superseded = request["generation"] != doc["generation"]
        if request["mode"] == "advance" and identifiers and identifiers == doc.get("last_page_ids"):
            continuity = False
        for source in sources:
            member = session.scalar(
                select(DiscoveryMembership).where(
                    DiscoveryMembership.feed_id == feed.id, DiscoveryMembership.source_id == source["id"]
                )
            )
            seen = {"last_seen_at": now(), "listing_run_id": row.id}
            if request["mode"] == "advance" and not superseded:
                seen["last_backfill_generation"] = request["generation"]
            if member is None:
                session.add(
                    DiscoveryMembership(
                        feed_id=feed.id, source_id=source["id"], document={"first_seen_at": now(), **seen}
                    )
                )
            else:
                member.document = {**member.document, **seen}
        session.flush()
        if request["mode"] == "advance" and not superseded:
            if not continuity:
                doc = {
                    **doc,
                    "backfill_state": "needs_reconcile",
                    "pause_reason": "Page anchors disappeared; restart from the head",
                    "cycle_remaining": 0,
                }
            elif response.get("index_limit_reached"):
                doc = {
                    **doc,
                    "backfill_state": "index_limit",
                    "cycle_remaining": 0,
                    "pause_reason": "10,000-position scope limit reached; archive end remains unknown",
                }
            elif response["visible_end_observed"]:
                members = list(
                    session.scalars(select(DiscoveryMembership).where(DiscoveryMembership.feed_id == feed.id))
                )
                unseen = [
                    m.source_id
                    for m in members
                    if m.document.get("last_backfill_generation") != request["generation"]
                ]
                for identifier in unseen:
                    source_row = session.get(Source, identifier)
                    source_row.document = {
                        **source_row.document,
                        "metadata_refresh_needed": True,
                        "not_observed_in_listing_run": row.id,
                    }
                doc = {
                    **doc,
                    "backfill_state": "visible_end_observed",
                    "cycle_remaining": 0,
                    "last_visible_end_at": now(),
                    "last_visible_end_generation": request["generation"],
                    "unseen_in_visible_pass_source_ids": unseen,
                    "absence_interpretation": "not_observed_in_visible_listing_not_proof_of_deletion",
                }
            else:
                step = response["raw_count"] - doc["config"]["overlap"]
                next_index = request["start"] + max(1, step)
                if next_index > 10000:
                    doc = {
                        **doc,
                        "backfill_state": "index_limit",
                        "pause_reason": "10,000-position limit reached",
                        "cycle_remaining": 0,
                    }
                else:
                    doc = {
                        **doc,
                        "next_index": next_index,
                        "last_page_ids": identifiers,
                        "anchors": identifiers[-doc["config"]["overlap"] :],
                        "cycle_remaining": max(0, doc["cycle_remaining"] - 1),
                    }
            feed.document = doc
        row.state, row.lease_until = "succeeded", None
        row.document = {
            **row.document,
            "finished_at": now(),
            "response_sha256": response["sha256"],
            "source_ids": list(dict.fromkeys(s["id"] for s in sources)),
            "source_count": len(set(identifiers)),
            "raw_entry_count": response["raw_count"],
            "skipped": response["skipped"],
            "visible_end_observed": response["visible_end_observed"],
            "continuity_supported": continuity,
            "superseded": superseded,
            "media_jobs_created": 0,
        }
        session.flush()
        return feed_view(session, feed)


def run_discovery_once(db):
    try:
        schedule_due(db)
        run = claim_discovery(db)
    except StaleDataError:
        return None
    if run is None:
        return None
    try:
        execute_discovery(db, run)
    except Exception as exc:
        with db.session() as session:
            row = session.get(DiscoveryRun, run["id"])
            if row.document.get("lease_token") == run["lease_token"]:
                paused = isinstance(exc, DiscoveryPaused)
                row.state, row.lease_until = "queued" if paused else "failed", None
                if paused:
                    row.attempts = max(0, row.attempts - 1)
                row.document = {
                    **row.document,
                    "error": str(exc),
                    "finished_at": now(),
                    "retry_at": now() if paused else later(60 * 2 ** (row.attempts - 1)),
                }
                if row.attempts >= 3:
                    feed = session.get(DiscoveryFeed, row.feed_id)
                    feed.document = {
                        **feed.document,
                        "backfill_state": "needs_attention",
                        "cycle_remaining": 0,
                        "pause_reason": "Discovery failed after 3 attempts",
                    }
        return {
            "id": run["id"],
            "state": "queued" if isinstance(exc, DiscoveryPaused) else "failed",
            "error": str(exc),
        }
    return {"id": run["id"], "state": "succeeded"}
