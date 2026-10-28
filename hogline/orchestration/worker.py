import json
import time
from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, uuid5

import numpy as np
from PIL import Image
from sqlalchemy import select, update
from sqlalchemy.orm.exc import StaleDataError

from hogline.adapters.sources import digest, video_link
from hogline.config import profile
from hogline.db import Evidence, Job, JobExecution, ProcessingBatch, Scan, now
from hogline.domain.catalog import propose_games, source_or_error
from hogline.domain.models import ScanRequest, uid
from hogline.media.frames import (
    EvidenceBudget,
    EvidenceLimitError,
    capture,
    observe,
    resolve_media,
    save_image,
    source_seconds,
)
from hogline.orchestration.plans import (
    WorkStopped,
    check_execution,
    enqueue_in_session,
    finish_execution,
    freeze_outcomes,
    payload_for,
)
from hogline.perception.pipeline import analyze_frame
from hogline.perception.stones import VERSION as DETECTOR_VERSION
from hogline.reconstruction.activity import VERSION, classify, propose


def enqueue(db, request: ScanRequest):
    with db.session() as session:
        source = source_or_error(session, request.source_id)
        if source.get("duration_seconds") and request.end_seconds > source["duration_seconds"]:
            raise ValueError("Scan exceeds source duration")
        return job_view(enqueue_in_session(session, payload_for(source, request)))


def job_view(job):
    return {
        "id": job.id,
        "state": job.state,
        "attempts": job.attempts,
        "lease_until": job.lease_until,
        **job.document,
    }


def jobs(db):
    with db.session() as session:
        return jobs_in_session(session)


def jobs_in_session(session):
    return [job_view(j) for j in session.scalars(select(Job))]


def claim(db):
    try:
        return claim_transaction(db)
    except StaleDataError:
        return None  # Another worker/control updated this batch; retry on the next poll.


def claim_transaction(db):
    with db.session() as session:
        expired = (Job.state == "running") & (Job.lease_until < now())
        for old in session.scalars(select(Job).where(expired)):
            execution = session.get(JobExecution, old.document.get("lease_token", ""))
            if execution and execution.state == "running":
                # Bound unknown work after a crash by one lease, rather than
                # silently dropping time incurred since the last heartbeat.
                execution.document = {**execution.document, "expiry_accounting": "one_lease_upper_bound"}
                finish_execution(
                    session, execution.id, "expired", execution.document.get("wall_seconds", 0) + 120
                )
            old.state = "failed" if old.attempts >= 3 else "queued"
            if old.document.get("batch_id"):
                owner = session.get(ProcessingBatch, old.document["batch_id"])
                if owner.state == "cancelled":
                    old.state = "cancelled"
                    freeze_outcomes(session, owner)
                elif old.state == "failed" and owner.state == "active":
                    owner.state = "paused"
                    owner.document = {
                        **owner.document,
                        "pause_reason": "A work lease expired after 3 attempts",
                    }
            old.lease_until = None
            old.document = {**old.document, "error": "Work lease expired"}
        session.flush()
        eligible = (Job.state == "queued") & (Job.attempts < 3)
        job, batch = None, None
        for candidate in session.scalars(select(Job).where(eligible).order_by(Job.id)):
            owner = (
                session.get(ProcessingBatch, candidate.document["batch_id"])
                if candidate.document.get("batch_id")
                else None
            )
            if owner and (owner.state != "active" or owner.document.get("active_execution")):
                continue
            job, batch = candidate, owner
            break
        if job is None:
            return None
        token = uid()
        until = (datetime.now(UTC) + timedelta(seconds=120)).isoformat()
        result = session.execute(
            update(Job)
            .where(Job.id == job.id, eligible)
            .values(
                state="running",
                attempts=Job.attempts + 1,
                lease_until=until,
                document={**job.document, "lease_token": token, "error": None},
            )
        )
        if result.rowcount != 1:
            return None
        session.add(
            JobExecution(
                id=token,
                job_id=job.id,
                batch_id=batch.id if batch else None,
                state="running",
                document={"started_at": now(), "wall_seconds": 0, "new_image_bytes_reserved": 0},
            )
        )
        if batch:
            batch.document = {**batch.document, "active_execution": token}
        session.refresh(job)
        return job_view(job)


def update_owned(db, job_id, token, **changes):
    with db.session() as session:
        job = session.get(Job, job_id)
        if job.state != "running" or job.document.get("lease_token") != token:
            raise ValueError("Work lease was reclaimed; this worker cannot publish")
        job.document = {**job.document, **changes}
        job.lease_until = (datetime.now(UTC) + timedelta(seconds=120)).isoformat()


def cached_frame(db, source, seconds, layout=None, acquisition_tier="evidence"):
    cache_key = {
        "source": source["id"],
        "metadata": source["metadata_sha256"],
        "second": seconds,
        "version": "original-pts-frame-v1",
    }
    if acquisition_tier == "coarse":
        cache_key["acquisition_tier"] = "coarse-v1"
    key = digest(cache_key)
    path = db.settings.data_dir / "cache" / f"{key}.json"
    if not path.exists() and acquisition_tier == "evidence":
        # Pre-acquisition-cache pilot keys included the profile/stage. Only inspect
        # exact keys pinned to this metadata snapshot, never arbitrary manifests
        # that happen to have the same source ID and timestamp.
        with db.session() as session:
            old_jobs = session.scalars(
                select(Job).where(
                    Job.document["request"]["source_id"].as_string() == source["id"],
                    Job.document["source_hash"].as_string() == source["metadata_sha256"],
                )
            )
            variants = {(j.document["profile_sha256"], j.document["stage_version"]) for j in old_jobs}
        for profile_hash, stage_version in variants:
            legacy_key = digest(
                {
                    "source": source["id"],
                    "metadata": source["metadata_sha256"],
                    "second": seconds,
                    "profile": profile_hash,
                    "version": stage_version,
                }
            )
            legacy = path.parent / f"{legacy_key}.json"
            try:
                candidate = json.loads(legacy.read_text())
                if (
                    candidate.get("source_id") == source["id"]
                    and candidate.get("acquisition_tier", "evidence") == acquisition_tier
                    and abs(
                        candidate.get("requested_seconds", candidate.get("source_seconds", -1000)) - seconds
                    )
                    < 0.001
                ):
                    path.write_text(json.dumps(candidate))
                    break
            except (OSError, ValueError, TypeError):
                continue
    if not path.exists():
        return None, path
    try:
        doc = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None, path
    if doc.get("source_metadata_sha256", source["metadata_sha256"]) != source["metadata_sha256"]:
        return None, path
    import hashlib

    for asset in [doc["image"]]:
        file = db.settings.data_dir / asset["path"]
        if not file.is_file() or hashlib.sha256(file.read_bytes()).hexdigest() != asset["sha256"]:
            return None, path
    doc["_recrop_required"] = doc.get("profile_sha256") != digest(layout or profile())
    for asset in doc["crops"].values():
        file = db.settings.data_dir / asset["path"]
        if not file.is_file() or hashlib.sha256(file.read_bytes()).hexdigest() != asset["sha256"]:
            doc["_recrop_required"] = True
    return doc, path


def vectors_from_doc(db, doc):
    return {
        name: np.asarray(
            Image.open(db.settings.data_dir / crop["path"]).convert("L").resize((96, 96)), dtype=np.float32
        )
        for name, crop in doc["crops"].items()
    }


def execute(db, job):
    request = ScanRequest.model_validate(job["request"])
    with db.session() as session:
        source = source_or_error(session, request.source_id)
    if source["metadata_sha256"] != job["source_hash"]:
        raise ValueError("Source manifest changed after enqueue. Enqueue a job against the current metadata.")
    if job["stage_version"] != VERSION or job["perception_version"] != DETECTOR_VERSION:
        raise ValueError(
            "Processing version changed; enqueue current work instead of changing an old job meaning."
        )
    layout = job.get("profile") or profile()
    token, started = job["lease_token"], time.monotonic()
    samples, previous, evidence_docs = [], None, {}
    media, media_selection, cache_hits, logical_bytes = None, {}, 0, 0
    budget = EvidenceBudget(db.settings.data_dir, request.max_evidence_mb, db.settings.max_cache_mb)
    budget.reserve_growth = lambda size: check_execution(db, token, time.monotonic() - started, size)
    count = int(np.ceil((request.end_seconds - request.start_seconds) / request.interval_seconds))
    for index in range(count):
        check_execution(db, token, time.monotonic() - started)
        if time.monotonic() - started > request.max_worker_seconds:
            raise ValueError(
                "Worker time budget reached. Retry to resume cached frames or shorten this work unit."
            )
        seconds = request.start_seconds + index * request.interval_seconds
        doc, cache_path = cached_frame(db, source, seconds, layout, request.acquisition_tier)
        if doc and not doc.pop("_recrop_required", False):
            vectors = vectors_from_doc(db, doc)
            cache_hits += 1
            for asset in [doc["image"], *doc["crops"].values()]:
                budget.consume(asset["bytes"])
        else:
            if doc:
                image = Image.open(db.settings.data_dir / doc["image"]["path"]).convert("RGB")
                timing = {name: doc[name] for name in ("pts", "time_base_num", "time_base_den")}
                media_selection = doc.get("media_selection", {})
                cache_hits += 1
                composite_asset = doc["image"]
                budget.consume(composite_asset["bytes"])
            else:
                if media is None:
                    handle = (
                        resolve_media(source, max_height=360)
                        if request.acquisition_tier == "coarse"
                        else resolve_media(source)
                    )
                    media = handle["uri"]
                    media_selection = {key: value for key, value in handle.items() if key != "uri"}
                image, timing = capture(media, seconds)
                decoded_size = list(image.size)
                if request.acquisition_tier == "coarse":
                    image.thumbnail((640, 360))
                media_selection = {
                    **media_selection,
                    "decoded_frame_size": decoded_size,
                    "retained_frame_size": list(image.size),
                }
                composite_asset = save_image(image, db.settings.data_dir, budget)
                # Persist acquisition before crop/perception work. A later quota/failure
                # can resume from this image without accessing the recording again.
                raw = {
                    "source_id": source["id"],
                    "source_metadata_sha256": source["metadata_sha256"],
                    **timing,
                    "source_seconds": source_seconds(timing, source),
                    "youtube_url": video_link(source, source_seconds(timing, source)),
                    "image": composite_asset,
                    "crops": {},
                    "profile_sha256": None,
                    "requested_seconds": seconds,
                    "acquisition_tier": request.acquisition_tier,
                    "media_selection": media_selection,
                }
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                raw_temp = cache_path.with_suffix(f".{token}.tmp")
                raw_temp.write_text(json.dumps(raw))
                raw_temp.replace(cache_path)
            doc, vectors = observe(
                image,
                timing,
                source,
                db.settings.data_dir,
                budget,
                layout,
                composite_asset=composite_asset,
                acquisition_tier=request.acquisition_tier,
            )
            doc["media_selection"] = media_selection
            doc["requested_seconds"] = seconds
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            temp = cache_path.with_suffix(f".{token}.tmp")
            temp.write_text(json.dumps(doc))
            temp.replace(cache_path)
        logical_bytes += sum(a["bytes"] for a in [doc["image"], *doc["crops"].values()])
        if logical_bytes > request.max_evidence_mb * 1024**2:
            raise ValueError("Evidence budget reached. Shorten the scan or explicitly raise its budget.")
        doc["id"] = str(uuid5(NAMESPACE_URL, doc["evidence_key"]))
        evidence_docs[doc["id"]] = doc
        observation = classify(previous, vectors)
        owner_start, owner_end = request.ownership
        samples.append(
            {
                "seconds": doc["source_seconds"],
                "requested_seconds": seconds,
                "owned": owner_start <= seconds < owner_end,
                "evidence_id": doc["id"],
                **observation,
            }
        )
        previous = vectors
        update_owned(
            db,
            job["id"],
            token,
            progress=(index + 1) / count,
            completed_frames=index + 1,
            cache_hits=cache_hits,
            evidence_bytes=logical_bytes,
        )
        check_execution(db, token, time.monotonic() - started)
    result = {
        "request": request.model_dump(),
        "source_metadata_sha256": source["metadata_sha256"],
        "version": VERSION,
        "profile": layout,
        "samples": samples,
        "candidates": propose(
            [s for s in samples if s["owned"]], *request.ownership, request.interval_seconds
        ),
        "processed_span_kind": "sparse_sampled",
        "clock_status": "unresolved",
        "wall_seconds": time.monotonic() - started,
        "cache_hits": cache_hits,
        "evidence_bytes": logical_bytes,
    }
    check_execution(db, token, time.monotonic() - started)
    with db.session() as session:
        row = session.get(Job, job["id"])
        if row.state != "running" or row.document.get("lease_token") != token:
            raise ValueError("Work lease expired before publication")
        for doc in evidence_docs.values():
            # Stable evidence IDs collapse frames shared between overlapping work units.
            if session.get(Evidence, doc["id"]) is None:
                session.add(
                    Evidence(
                        id=doc["id"], source_id=source["id"], evidence_key=doc["evidence_key"], document=doc
                    )
                )
        session.flush()
        if request.acquisition_tier == "evidence":
            for doc in evidence_docs.values():
                analyze_frame(session, session.get(Evidence, doc["id"]), db.settings.data_dir)
        result["game_ids"] = propose_games(session, source, result, row.id) if request.propose_games else []
        session.add(Scan(job_id=row.id, source_id=source["id"], document=result))
        row.state, row.lease_until = "succeeded", None
        row.document = {
            **row.document,
            "finished_at": now(),
            "progress": 1,
            "wall_seconds": result["wall_seconds"],
        }
        finish_execution(session, token, "succeeded", time.monotonic() - started)
    return result


def run_one(db):
    job = claim(db)
    if job is None:
        return None
    started = time.monotonic()
    try:
        execute(db, job)
    except Exception as exc:
        with db.session() as session:
            row = session.get(Job, job["id"])
            if row.document.get("lease_token") == job["lease_token"]:
                batch = (
                    session.get(ProcessingBatch, row.document["batch_id"])
                    if row.document.get("batch_id")
                    else None
                )
                stopped = isinstance(exc, WorkStopped) or (
                    batch and isinstance(exc, EvidenceLimitError) and exc.scope == "shared"
                )
                if stopped:
                    if batch and batch.state == "active":
                        batch.state = "paused"
                        batch.document = {**batch.document, "pause_reason": str(exc)}
                    row.state = "cancelled" if batch and batch.state == "cancelled" else "queued"
                    row.attempts = max(0, row.attempts - 1)  # Operator pauses do not consume failure retries.
                else:
                    row.state = "failed"
                row.lease_until = None
                row.document = {**row.document, "error": str(exc), "finished_at": now()}
                finish_execution(
                    session,
                    job["lease_token"],
                    "stopped" if stopped else "failed",
                    time.monotonic() - started,
                )
                outcome = row.state
            else:
                outcome = "lease_lost"
        return {"id": job["id"], "state": outcome, "error": str(exc)}
    return {"id": job["id"], "state": "succeeded"}


def retry(db, job_id):
    with db.session() as session:
        job = session.get(Job, job_id)
        if job is None or job.state != "failed":
            raise ValueError("Only failed jobs can be retried; successful jobs are already cached")
        if job.attempts >= 3:
            raise ValueError(
                "Retry limit (3 attempts) reached; inspect the source before enqueueing new work"
            )
        job.state = "queued"
        job.document = {**job.document, "error": None, "progress": 0}
        return job_view(job)
