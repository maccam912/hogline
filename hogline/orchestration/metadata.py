"""Bounded metadata upgrades through the existing durable worker; never media."""

import time

from pydantic import Field
from sqlalchemy import select

from hogline.adapters.sources import curated_metadata, digest, run_json, upsert_source_in_session
from hogline.db import Job, Source, now
from hogline.domain.catalog import source_or_error
from hogline.domain.models import Model
from hogline.orchestration.plans import enqueue_in_session, finish_execution

VERSION = "bounded-metadata-refresh-v1"


class MetadataRefreshRequest(Model):
    limit: int = Field(default=4, ge=1, le=16)
    timeout_seconds: int = Field(default=30, ge=5, le=60)


def source_pin(source):
    # Full and listing snapshots, plus a prior refresh with identical content,
    # all matter when deciding whether an in-flight response is still current.
    return digest(source)


def metadata_queue(session):
    pending = [
        row.document
        for row in session.scalars(select(Source).order_by(Source.id))
        if row.document.get("platform") == "youtube" and row.document.get("metadata_refresh_needed", False)
    ]
    jobs = [
        {"id": row.id, "state": row.state, "attempts": row.attempts, **row.document}
        for row in session.scalars(select(Job))
        if row.document.get("kind") == "metadata_refresh"
    ]
    jobs.sort(key=lambda j: (j["created_at"], j["id"]), reverse=True)
    return {"pending_sources": pending, "jobs": jobs[:50], "total_jobs": len(jobs)}


def enqueue_refresh(db, request):
    with db.snapshot_session(write=True) as session:
        queue = metadata_queue(session)
        active = {
            j.document["request"]["source_id"]
            for j in session.scalars(select(Job))
            if j.document.get("kind") == "metadata_refresh" and j.state in ("queued", "running")
        }
        existing = {
            (j.document["request"]["source_id"], j.document.get("source_pin"))
            for j in session.scalars(select(Job))
            if j.document.get("kind") == "metadata_refresh" and j.document.get("stage_version") == VERSION
        }
        selected = [
            s
            for s in queue["pending_sources"]
            if s["id"] not in active and (s["id"], source_pin(s)) not in existing
        ][: request.limit]
        result = []
        for source in selected:
            job = enqueue_in_session(
                session,
                {
                    "kind": "metadata_refresh",
                    "stage_version": VERSION,
                    "source_hash": source["metadata_sha256"],
                    "source_pin": source_pin(source),
                    "request": {"source_id": source["id"], "timeout_seconds": request.timeout_seconds},
                },
            )
            result.append({"id": job.id, "state": job.state, "source_id": source["id"]})
        return {
            "jobs": result,
            "pending_count": len(queue["pending_sources"]),
            "maximum_request_seconds": len(result) * request.timeout_seconds,
            "notice": "Metadata only. Each request has its own timeout; failed work requires explicit retry.",
        }


def execute_metadata(db, job):
    started = time.monotonic()
    if job["stage_version"] != VERSION:
        raise ValueError("Metadata refresh method changed; queue current work")
    with db.session() as session:
        source = source_or_error(session, job["request"]["source_id"])
    if source_pin(source) != job["source_pin"]:
        raise ValueError("Source changed after metadata refresh was queued; queue current work")
    raw = run_json(
        [
            "yt-dlp",
            "--ignore-config",
            "--skip-download",
            "--no-playlist",
            "--socket-timeout",
            "10",
            "--retries",
            "0",
            "--dump-single-json",
            f"https://www.youtube.com/watch?v={source['platform_id']}",
        ],
        timeout=job["request"]["timeout_seconds"],
    )
    if raw.get("id") != source["platform_id"]:
        raise ValueError("Returned metadata ID differs from the queued source")
    clean = curated_metadata(raw)
    # Commit the manifest and successful job together. A reclaimed lease or
    # concurrent import/listing must not publish an obsolete response.
    with db.snapshot_session(write=True) as session:
        row = session.get(Job, job["id"])
        current = source_or_error(session, source["id"])
        if row.state != "running" or row.document.get("lease_token") != job["lease_token"]:
            raise ValueError("Metadata refresh lease was reclaimed")
        if source_pin(current) != job["source_pin"]:
            raise ValueError("Source changed during metadata refresh; queue current work")
        updated = upsert_source_in_session(session, f"youtube:{source['platform_id']}", clean, "youtube")
        elapsed = time.monotonic() - started
        row.state, row.lease_until = "succeeded", None
        row.document = {
            **row.document,
            "progress": 1,
            "finished_at": now(),
            "wall_seconds": elapsed,
            "result_metadata_sha256": updated["metadata_sha256"],
            "manifest_changed": source["metadata_sha256"] != updated["metadata_sha256"],
        }
        finish_execution(session, job["lease_token"], "succeeded", elapsed)
    return updated
