"""Automatic face lineups for analyzed recordings.

When a recording analysis finishes, the recording joins a small queue. A scheduler that runs
with the workers turns the next waiting recording into lineup batches — one per posted game
span (score history padded like the manual span suggestion), or bounded pieces of the whole
recording when no score was read — with at most `auto_lineup_slots` recordings at once.
Recordings analyzed earlier join only when the operator asks (backfill). Titles that suggest
junior events are skipped. Every batch is an ordinary lineup batch, visible under Jobs.
"""

import logging
import math
import time

from sqlalchemy import select
from sqlalchemy.orm.exc import StaleDataError

from hogline.config import pretrained_model_path
from hogline.db import AutoLineup, GameLineup, Job, ProcessingBatch, Source, now, queue_field
from hogline.domain.catalog import source_or_error
from hogline.media.post_live import has_verified_download
from hogline.orchestration.lineups import (
    ASSEMBLY_KIND,
    KIND,
    UNFINISHED_RECORDING,
    LineupRequest,
    lineup_batch_in_session,
    lineup_models,
)

VERSION = "automatic-game-lineups-v1"
PAD_BEFORE_SECONDS = 1200
PAD_AFTER_SECONDS = 600
MIN_RECORDING_SECONDS = 900
MAX_PIECE_SECONDS = 3 * 3600
AUTO_THUMBNAILS = 96
CHECK_SECONDS = 20
OPEN_STATES = ("waiting", "running", "attention")
# Rough planning figures from the October 30 2026 full-recording lineup (1 s sampling:
# 0.9 worker-seconds per recording second); 2 s sampling roughly halves detection work.
WORKER_SECONDS_PER_SECOND = {1.0: 0.9, 2.0: 0.55}
STORAGE_MB_PER_HOUR = 18

log = logging.getLogger(__name__)
_next_check = 0.0
_status_cache = {"at": -1e9, "value": None}


def game_spans(duration, scorecards):
    """Game intervals for one recording: padded score histories, else the whole recording."""
    spans = []
    for card in scorecards:
        start = max(0.0, float(card["first_seconds"]) - PAD_BEFORE_SECONDS)
        end = min(float(duration), float(card["last_seconds"]) + PAD_AFTER_SECONDS)
        if end > start:
            spans.append([start, end])
    spans.sort()
    merged = []
    for span in spans:
        if merged and span[0] <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], span[1])
        else:
            merged.append(span)
    basis = "posted_score_histories" if merged else "whole_recording"
    if not merged:
        merged = [[0.0, float(duration)]]
    out = []
    for start, end in merged:
        pieces = max(1, math.ceil((end - start) / MAX_PIECE_SECONDS))
        step = (end - start) / pieces
        for k in range(pieces):
            a, b = start + k * step, end if k == pieces - 1 else start + (k + 1) * step
            if b - a >= 60:
                out.append((round(a, 3), round(b, 3)))
    return out, basis


def _history(row, event, **details):
    doc = dict(row.document)
    doc["history"] = [*doc.get("history", []), {"at": now(), "event": event, **details}][-20:]
    return doc


def _set(row, state, event, **details):
    row.document = {**_history(row, event, **details), **details}
    row.state = state


def request_in_session(session, source_id, reason):
    """Queue one recording once; an existing outcome or operator decision stands.

    A newly finished analysis is new information: entries that needed attention, were
    cancelled or were skipped automatically are queued again. Operator skips stand.
    """
    row = session.get(AutoLineup, source_id)
    if row is not None:
        operator_skip = (
            row.document.get("history") and row.document["history"][-1]["event"] == "skipped_by_operator"
        )
        if reason == "recording_analysis" and (
            row.state in ("attention", "cancelled") or (row.state == "skipped" and not operator_skip)
        ):
            _set(row, "waiting", "queued_after_analysis", reason=reason, requested_at=now(), problem=None)
        return row
    row = AutoLineup(
        source_id=source_id,
        state="waiting",
        document={"reason": reason, "requested_at": now(), "version": VERSION, "history": []},
    )
    session.add(row)
    session.flush()
    return row


def analyzed_source_ids(session):
    """Recordings with a completed whole-recording analysis (no batch documents loaded)."""
    source = ProcessingBatch.document["request"]["source_ids"][0].as_string()
    return set(
        session.scalars(
            select(source).where(
                queue_field(ProcessingBatch, "campaign_purpose") == "recording_analysis",
                ProcessingBatch.state == "completed",
            )
        )
    )


def sources_with_lineups(session):
    lineup_sources = set(session.scalars(select(GameLineup.source_id)))
    source = ProcessingBatch.document["lineup"]["source_id"].as_string()
    batch_sources = set(
        session.scalars(
            select(source).where(
                queue_field(ProcessingBatch, "kind") == KIND,
                ProcessingBatch.state.in_(["active", "paused", "completed"]),
            )
        )
    )
    return lineup_sources | batch_sources


def backfill(db, source_ids=None):
    """Queue analyzed recordings that have no lineup yet (all of them, or the ones given)."""
    with db.snapshot_session(write=True) as session:
        analyzed = analyzed_source_ids(session)
        covered = sources_with_lineups(session)
        queued = set(session.scalars(select(AutoLineup.source_id)))
        wanted = analyzed if source_ids is None else set(source_ids)
        result = {"queued": 0, "already_queued": 0, "has_lineup": 0, "not_analyzed": 0}
        for source_id in sorted(wanted):
            if source_id not in analyzed:
                result["not_analyzed"] += 1
            elif source_id in queued:
                result["already_queued"] += 1
            elif source_id in covered:
                result["has_lineup"] += 1
            else:
                request_in_session(session, source_id, "backfill")
                result["queued"] += 1
    _status_cache["at"] = -1e9
    return result


def _outcome(session, row):
    """Project a running request from its lineup batches; returns (state, details)."""
    batch_ids = row.document.get("batch_ids", [])
    batches = [session.get(ProcessingBatch, b) for b in batch_ids]
    have = set(session.scalars(select(GameLineup.batch_id).where(GameLineup.batch_id.in_(batch_ids))))
    if batch_ids and all(b in have for b in batch_ids):
        return "done", {"lineups": len(have), "problem": None}
    problems = []
    for batch in batches:
        if batch is None:
            problems.append("A lineup batch is missing")
        elif batch.state in ("paused", "failed"):
            problems.append(batch.document.get("pause_reason") or f"A lineup batch is {batch.state}")
    for job in session.scalars(
        select(Job).where(Job.state == "failed", queue_field(Job, "kind") == ASSEMBLY_KIND)
    ):
        if job.document.get("lineup_batch_id") in batch_ids:
            problems.append(job.document.get("error") or "Face grouping failed")
    if problems:
        return "attention", {"problem": problems[0]}
    if all(b is not None and (b.id in have or b.state == "cancelled") for b in batches):
        return "cancelled", {"lineups": len(have), "problem": None}
    return "running", {"problem": None}


def refresh_open(session):
    for row in session.scalars(select(AutoLineup).where(AutoLineup.state.in_(["running", "attention"]))):
        state, details = _outcome(session, row)
        if state != row.state:
            _set(row, state, f"became_{state}", **details)


def _scorecards(session, source_id):
    batch = session.scalars(
        select(ProcessingBatch)
        .where(
            queue_field(ProcessingBatch, "campaign_purpose") == "recording_analysis",
            ProcessingBatch.state == "completed",
            ProcessingBatch.document["request"]["source_ids"][0].as_string() == source_id,
        )
        .order_by(queue_field(ProcessingBatch, "created_at").desc())
    ).first()
    result = (batch.document.get("analysis_result") or {}) if batch is not None else {}
    return (result.get("scorecard_reconstruction") or {}).get("scorecards") or []


def worker_budget(seconds, interval):
    rate = WORKER_SECONDS_PER_SECOND.get(float(interval), 0.9 / max(1.0, interval) + 0.1)
    return int(min(86400, max(1800, 1.5 * rate * seconds)))


def start_recording(session, row, settings, embedder, face_version):
    source = source_or_error(session, row.source_id)
    title = (source.get("title") or "").lower()
    excluded = next((w for w in settings.auto_lineup_exclude if w and w in title), None)
    if excluded:
        return _set(
            row, "skipped", "skipped", skip_reason=f"Title contains “{excluded}” (possible junior event)"
        )
    duration = source.get("duration_seconds")
    if not duration or (
        source.get("live_status") in ("is_live", "is_upcoming")
        or (source.get("live_status") == "post_live" and not has_verified_download(source))
    ):
        # Ask for fresh metadata; the recording can be queued again once it is complete.
        stored = session.get(Source, row.source_id)
        stored.document = {**stored.document, "metadata_refresh_needed": True}
        return _set(row, "attention", "unfinished_recording", problem=UNFINISHED_RECORDING)
    if duration < MIN_RECORDING_SECONDS:
        return _set(row, "skipped", "skipped", skip_reason="Recording is shorter than 15 minutes")
    if row.source_id in sources_with_lineups(session):
        return _set(row, "done", "existing_lineup", note="This recording already had a lineup")
    spans, basis = game_spans(duration, _scorecards(session, row.source_id))
    batch_ids = []
    try:
        with session.begin_nested():
            for start, end in spans:
                request = LineupRequest(
                    source_id=row.source_id,
                    start_seconds=start,
                    end_seconds=end,
                    interval_seconds=settings.auto_lineup_interval,
                    parallelism=settings.auto_lineup_parallelism,
                    max_thumbnails=AUTO_THUMBNAILS,
                    max_worker_seconds=worker_budget(end - start, settings.auto_lineup_interval),
                )
                view = lineup_batch_in_session(
                    session,
                    request,
                    embedder,
                    face_version,
                    automatic={"version": VERSION, "basis": basis, "reason": row.document.get("reason")},
                )
                batch_ids.append(view["id"])
    except ValueError as exc:
        return _set(row, "attention", "start_failed", problem=str(exc))
    _set(
        row,
        "running",
        "started",
        batch_ids=batch_ids,
        spans=[list(s) for s in spans],
        basis=basis,
        interval_seconds=settings.auto_lineup_interval,
        started_at=now(),
        problem=None,
    )


def _next_waiting(session, limit):
    rows = list(session.scalars(select(AutoLineup).where(AutoLineup.state == "waiting")))
    if not rows:
        return []
    dates = {
        s.id: s.document.get("title_date") or ""
        for s in session.scalars(select(Source).where(Source.id.in_([r.source_id for r in rows])))
    }
    # Fresh analyses first (oldest request first), then backfill from the newest games back.
    fresh = sorted(
        (r for r in rows if r.document.get("reason") != "backfill"), key=lambda r: r.document["requested_at"]
    )
    older = sorted(
        (r for r in rows if r.document.get("reason") == "backfill"),
        key=lambda r: (dates.get(r.source_id, ""), r.source_id),
        reverse=True,
    )
    return (fresh + older)[:limit]


_blocked = {"reason": None}


def models_problem(root):
    """Why automatic lineups cannot start (models), checked without loading networks."""
    import os
    from pathlib import Path

    from hogline.perception.faces import installed_face_version

    try:
        installed_face_version(root)
    except ValueError as exc:
        return str(exc)
    person = Path(
        os.environ.get(
            "HOGLINE_PERSON_MODEL",
            pretrained_model_path(root, "yolox-s-coco/object_detection_yolox_2022nov.onnx"),
        )
    )
    if not person.is_file():
        return "The pinned person model is not installed"
    return None


def advance_auto_lineups(db, force=False):
    """Start the next waiting recordings when a slot is free. Safe to call from every worker."""
    global _next_check
    settings = db.settings
    if not settings.auto_lineups:
        return None
    if not force and time.monotonic() < _next_check:
        return None
    _next_check = time.monotonic() + CHECK_SECONDS
    try:
        with db.snapshot_session() as session:
            # Most polls find nothing open; only then is the write lock worth taking.
            if (
                session.scalar(select(AutoLineup.source_id).where(AutoLineup.state.in_(OPEN_STATES)).limit(1))
                is None
            ):
                return None
        with db.snapshot_session(write=True) as session:
            refresh_open(session)
            running = len(
                list(session.scalars(select(AutoLineup.source_id).where(AutoLineup.state == "running")))
            )
            free = settings.auto_lineup_slots - running
            if free <= 0 or not _next_waiting(session, 1):
                return None
        try:
            embedder, face_version = lineup_models(settings.data_dir)
            _blocked["reason"] = None
        except ValueError as exc:
            _blocked["reason"] = str(exc)
            return None
        started = []
        with db.snapshot_session(write=True) as session:
            refresh_open(session)
            # Skipped or already-covered recordings free their turn at once; bound one pass.
            for _ in range(200):
                running = len(
                    list(session.scalars(select(AutoLineup.source_id).where(AutoLineup.state == "running")))
                )
                if running >= settings.auto_lineup_slots:
                    break
                waiting = _next_waiting(session, 1)
                if not waiting:
                    break
                start_recording(session, waiting[0], settings, embedder, face_version)
                session.flush()
                started.append({"source_id": waiting[0].source_id, "state": waiting[0].state})
        _status_cache["at"] = -1e9
        return started
    except StaleDataError:
        return None


def control(db, source_id, action):
    """Operator actions on one recording's queue entry: queue it (again) or skip it."""
    with db.snapshot_session(write=True) as session:
        source_or_error(session, source_id)
        row = session.get(AutoLineup, source_id)
        if action == "queue":
            if row is None:
                row = request_in_session(session, source_id, "operator")
            elif row.state == "done":
                if session.scalar(select(GameLineup.id).where(GameLineup.source_id == source_id).limit(1)):
                    raise ValueError("This recording already has automatic lineups; delete them to run again")
                _set(
                    row, "waiting", "queued_by_operator", reason="operator", requested_at=now(), problem=None
                )
            elif row.state in ("skipped", "cancelled", "attention"):
                live = [
                    batch
                    for batch in (session.get(ProcessingBatch, b) for b in row.document.get("batch_ids", []))
                    if batch is not None and batch.state in ("active", "paused")
                ]
                if live:
                    raise ValueError(
                        "Resume, retry or cancel this recording's lineup batches under Jobs first"
                    )
                _set(
                    row, "waiting", "queued_by_operator", reason="operator", requested_at=now(), problem=None
                )
        elif action == "skip":
            if row is None:
                row = request_in_session(session, source_id, "operator")
            if row.state == "running":
                raise ValueError("Cancel this recording's running lineup batches under Jobs first")
            _set(row, "skipped", "skipped_by_operator", skip_reason="Skipped by the operator")
        else:
            raise ValueError("Unknown action")
        _status_cache["at"] = -1e9
        return {"source_id": source_id, "state": row.state}


def status(db, settings=None):
    """Queue summary for the Lineups page; the analyzed-recording scan is cached briefly."""
    settings = settings or db.settings
    with db.snapshot_session() as session:
        rows = list(session.scalars(select(AutoLineup)))
        counts = {}
        for row in rows:
            counts[row.state] = counts.get(row.state, 0) + 1
        sources = {
            s.id: s.document
            for s in session.scalars(
                select(Source).where(
                    Source.id.in_(
                        [r.source_id for r in rows if r.state in ("running", "attention", "skipped")]
                    )
                )
            )
        }
        if time.monotonic() - _status_cache["at"] > 300 or _status_cache["value"] is None:
            analyzed = analyzed_source_ids(session)
            missing = sorted(analyzed - sources_with_lineups(session) - {r.source_id for r in rows})
            durations = {
                s.id: s.document.get("duration_seconds") or 0
                for s in session.scalars(select(Source).where(Source.id.in_(missing)))
            }
            _status_cache.update(
                at=time.monotonic(),
                value={
                    "analyzed_recordings": len(analyzed),
                    "backfill_candidates": len(missing),
                    "backfill_hours": round(sum(durations.values()) / 3600, 1),
                },
            )
        backfill_view = dict(_status_cache["value"])

    def entry(row):
        source = sources.get(row.source_id, {})
        return {
            "source_id": row.source_id,
            "title": source.get("title"),
            "title_date": source.get("title_date"),
            "state": row.state,
            **{
                k: row.document.get(k)
                for k in ("reason", "problem", "basis", "spans", "batch_ids", "requested_at", "started_at")
            },
            "skip_reason": row.document.get("skip_reason"),
        }

    interval = settings.auto_lineup_interval
    hours = backfill_view["backfill_hours"]
    rate = WORKER_SECONDS_PER_SECOND.get(float(interval), 0.9 / max(1.0, interval) + 0.1)
    return {
        "enabled": settings.auto_lineups,
        "slots": settings.auto_lineup_slots,
        "parallelism": settings.auto_lineup_parallelism,
        "interval_seconds": interval,
        "excluded_title_words": list(settings.auto_lineup_exclude),
        "blocked": models_problem(settings.data_dir),
        "counts": counts,
        "running": [entry(r) for r in rows if r.state == "running"],
        "attention": [entry(r) for r in rows if r.state == "attention"],
        "skipped": [entry(r) for r in rows if r.state == "skipped"][:50],
        "backfill": {
            **backfill_view,
            "estimated_worker_hours": round(hours * rate, 1),
            "estimated_storage_gb": round(hours * STORAGE_MB_PER_HOUR / 1024, 1),
        },
    }


def advance_safely(db):
    try:
        return advance_auto_lineups(db)
    except Exception:  # Face scheduling must never stop ordinary work from being claimed.
        log.exception("Automatic lineup scheduling will retry")
        return None
