import json
import time
from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, uuid5

import numpy as np
from PIL import Image
from sqlalchemy import or_, select, update

from hogline.adapters.sources import digest
from hogline.config import profile
from hogline.db import Evidence, Job, Scan, now
from hogline.domain.catalog import propose_games, source_or_error
from hogline.domain.models import ScanRequest, uid
from hogline.media.frames import EvidenceBudget, capture, observe, resolve_media
from hogline.perception.pipeline import analyze_frame
from hogline.perception.stones import VERSION as DETECTOR_VERSION
from hogline.reconstruction.activity import VERSION, classify, propose


def enqueue(db, request: ScanRequest):
    with db.session() as session:
        source = source_or_error(session, request.source_id)
        if source.get("duration_seconds") and request.end_seconds > source["duration_seconds"]:
            raise ValueError("Scan exceeds source duration")
        layout = profile()
        payload = {
            "request": request.model_dump(),
            "stage_version": VERSION,
            "profile_sha256": digest(layout),
            "profile": layout,
            "source_hash": source["metadata_sha256"],
            "perception_version": DETECTOR_VERSION,
        }
        key = digest(payload)
        job = session.scalar(select(Job).where(Job.job_key == key))
        if job is None:
            job = Job(job_key=key, document={**payload, "created_at": now(), "progress": 0})
            session.add(job)
            session.flush()
        return job_view(job)


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
        return [job_view(j) for j in session.scalars(select(Job))]


def claim(db):
    with db.session() as session:
        expired = (Job.state == "running") & (Job.lease_until < now())
        for exhausted in session.scalars(select(Job).where(expired, Job.attempts >= 3)):
            exhausted.state = "failed"
            exhausted.lease_until = None
            exhausted.document = {**exhausted.document, "error": "Work lease expired after 3 attempts"}
        eligible = or_(Job.state == "queued", expired) & (Job.attempts < 3)
        job = session.scalar(select(Job).where(eligible).order_by(Job.id).limit(1))
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
        session.refresh(job)
        return job_view(job)


def update_owned(db, job_id, token, **changes):
    with db.session() as session:
        job = session.get(Job, job_id)
        if job.state != "running" or job.document.get("lease_token") != token:
            raise ValueError("Work lease was reclaimed; this worker cannot publish")
        job.document = {**job.document, **changes}
        job.lease_until = (datetime.now(UTC) + timedelta(seconds=120)).isoformat()


def cached_frame(db, source, seconds, layout=None):
    key = digest(
        {
            "source": source["id"],
            "metadata": source["metadata_sha256"],
            "second": seconds,
            "profile": digest(layout or profile()),
            "version": VERSION,
        }
    )
    path = db.settings.data_dir / "cache" / f"{key}.json"
    if not path.exists():
        return None, path
    try:
        doc = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None, path
    import hashlib

    for asset in [doc["image"], *doc["crops"].values()]:
        file = db.settings.data_dir / asset["path"]
        if not file.is_file() or hashlib.sha256(file.read_bytes()).hexdigest() != asset["sha256"]:
            return None, path
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
    layout = job.get("profile") or profile()
    token, started = job["lease_token"], time.monotonic()
    samples, previous, evidence_docs = [], None, {}
    media, media_selection, cache_hits, logical_bytes = None, {}, 0, 0
    budget = EvidenceBudget(db.settings.data_dir, request.max_evidence_mb, db.settings.max_cache_mb)
    count = int(np.ceil((request.end_seconds - request.start_seconds) / request.interval_seconds))
    for index in range(count):
        if time.monotonic() - started > request.max_worker_seconds:
            raise ValueError(
                "Worker time budget reached. Retry to resume cached frames or shorten this work unit."
            )
        seconds = request.start_seconds + index * request.interval_seconds
        doc, cache_path = cached_frame(db, source, seconds, layout)
        if doc:
            vectors = vectors_from_doc(db, doc)
            cache_hits += 1
            for asset in [doc["image"], *doc["crops"].values()]:
                budget.consume(asset["bytes"])
        else:
            if media is None:
                handle = resolve_media(source)
                media = handle["uri"]
                media_selection = {key: value for key, value in handle.items() if key != "uri"}
            image, timing = capture(media, seconds)
            doc, vectors = observe(image, timing, source, db.settings.data_dir, budget, layout)
            doc["media_selection"] = media_selection
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
        samples.append({"seconds": doc["source_seconds"], "evidence_id": doc["id"], **observation})
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
    result = {
        "request": request.model_dump(),
        "version": VERSION,
        "profile": layout,
        "samples": samples,
        "candidates": propose(samples, request.start_seconds, request.end_seconds, request.interval_seconds),
        "processed_span_kind": "sparse_sampled",
        "clock_status": "unresolved",
        "wall_seconds": time.monotonic() - started,
        "cache_hits": cache_hits,
        "evidence_bytes": logical_bytes,
    }
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
        for doc in evidence_docs.values():
            analyze_frame(session, session.get(Evidence, doc["id"]), db.settings.data_dir)
        result["game_ids"] = propose_games(session, source, result, row.id)
        session.add(Scan(job_id=row.id, source_id=source["id"], document=result))
        row.state, row.lease_until = "succeeded", None
        row.document = {
            **row.document,
            "finished_at": now(),
            "progress": 1,
            "wall_seconds": result["wall_seconds"],
        }
    return result


def run_one(db):
    job = claim(db)
    if job is None:
        return None
    try:
        execute(db, job)
    except Exception as exc:
        with db.session() as session:
            row = session.get(Job, job["id"])
            if row.document.get("lease_token") == job["lease_token"]:
                row.state, row.lease_until = "failed", None
                row.document = {**row.document, "error": str(exc), "finished_at": now()}
        return {"id": job["id"], "state": "failed", "error": str(exc)}
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
