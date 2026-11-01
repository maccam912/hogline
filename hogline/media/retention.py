"""Bounded review images after recording reconstruction; measurements and provenance survive."""

import json

from sqlalchemy import select

from hogline.config import profile
from hogline.db import (
    AnalysisWindow,
    BoardLabels,
    Evidence,
    FrameLabels,
    FrameObservation,
    GameRow,
    HouseLabels,
    Job,
    ProcessingBatch,
    SkillDeliveryRay,
    SkillEstimateRevision,
    SkillMotion,
    SkillSequence,
    Source,
    now,
    queue_field,
)
from hogline.media.frames import _shared_cache_usage, cache_size

POLICY = "bounded-recording-images-v1"
REVIEW_BYTES = 128 * 1024**2


def image_paths(value):
    """Include a derived crop's parent; only native image paths may be removed."""
    found = set()
    if isinstance(value, dict):
        name = value.get("path")
        if isinstance(name, str) and name.startswith("evidence/") and name.endswith(".png"):
            found.add(name)
        for child in value.values():
            found.update(image_paths(child))
    elif isinstance(value, list):
        for child in value:
            found.update(image_paths(child))
    return found


def checked_path(root, name):
    path = (root / name).resolve()
    if not path.is_relative_to((root / "evidence").resolve()):
        raise ValueError("Retention image path escapes the evidence archive")
    return path


def protected_evidence(session):
    """Human labels, reviewed games and pinned header references are never evicted."""
    ids = set(session.scalars(select(FrameLabels.evidence_id)))
    ids.update(session.scalars(select(BoardLabels.id)))
    for row in session.scalars(select(SkillEstimateRevision)):
        ids.update(row.document.get("frame_pins", {}))
    for row in session.scalars(select(SkillDeliveryRay)):
        ids.update(row.document.get("frame_pins", {}))
    # Independent visual decisions must remain inspectable after routine cache cleanup.
    for row in session.scalars(
        select(FrameObservation).where(
            FrameObservation.model_version.startswith("independent-visual-ice-end-v1")
        )
    ):
        ids.update(row.document.get("evidence_refs", {}))
    # House labels refer to complete windows, including their original anchors.
    for row in session.scalars(select(HouseLabels)):
        window = session.get(AnalysisWindow, row.window_id)
        if window:
            ids.update(window.document.get("anchor_evidence_ids", []))
    for game in session.scalars(select(GameRow)):
        if game.document.get("acceptance") != "human_confirmed":
            continue
        for end in game.document.get("ends", []):
            ids.update(end.get("evidence_ids", []))
        ids.update(game.document.get("result", {}).get("evidence_ids", []))
    layout = profile().get("broadcast", {})
    templates = [*layout.get("scoreboards", {}).values()]
    if layout.get("automatic_board_header"):
        templates.append(layout["automatic_board_header"]["reference"])
    for template in templates:
        reference = template.get("layout_reference", {})
        if not reference:
            continue
        source = session.scalar(select(Source).where(Source.source_key == reference["source_key"]))
        if source:
            ids.update(
                session.scalars(
                    select(Evidence.id).where(
                        Evidence.source_id == source.id,
                        Evidence.document["source_seconds"]
                        .as_float()
                        .between(
                            reference["source_seconds"] - 0.1,
                            reference["source_seconds"] + 0.1,
                        ),
                    )
                )
            )
    return ids


def pending_cleanup_query():
    # Do not deserialize every completed report and its task payloads on every worker poll.
    return select(ProcessingBatch).where(
        ProcessingBatch.state.in_(["completed", "failed"]),
        queue_field(ProcessingBatch, "storage_policy") == POLICY,
        queue_field(ProcessingBatch, "image_cleanup").is_(None),
    )


def cleanup_completed(session, root, review_bytes=REVIEW_BYTES):
    """Called under the writer transaction, between jobs, before admitting the next source.

    No files are deleted while a worker is running. Descriptors, checksums, PTS,
    observations, columnar data and immutable reconstruction reports are preserved.
    """
    pending = list(session.scalars(pending_cleanup_query()))
    if not pending or session.scalar(select(Job.id).where(Job.state == "running").limit(1)):
        return
    frames = list(session.scalars(select(Evidence)))
    observations = list(session.scalars(select(FrameObservation)))
    protected_ids = protected_evidence(session)
    live_sources = set()
    for batch in session.scalars(
        select(ProcessingBatch).where(ProcessingBatch.state.in_(["active", "paused"]))
    ):
        live_sources.update(batch.document.get("request", {}).get("source_ids", []))
    for job in session.scalars(select(Job).where(Job.state.in_(["queued", "running"]))):
        # Cancelled owners cannot require an acquisition again.
        source_id = job.document.get("request", {}).get("source_id")
        if source_id:
            live_sources.add(source_id)
    protected_ids.update(f.id for f in frames if f.source_id in live_sources)
    protected_paths = set()
    # Published skill evidence has its own owners rather than Evidence rows.
    # It is a bounded, reviewable result, not an orphan from a crashed worker.
    for model in (SkillSequence, SkillMotion):
        for document in session.scalars(select(model.document)):
            protected_paths.update(image_paths(document))
    for frame in frames:
        if frame.id in protected_ids:
            protected_paths.update(image_paths(frame.document))
    for observation in observations:
        if observation.evidence_id in protected_ids:
            protected_paths.update(image_paths(observation.document))
    # Partially completed scans can own images before publishing their Evidence rows.
    cached = []
    for path in (root / "cache").glob("*.json"):
        try:
            doc = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        cached.append((path, doc))
        if doc.get("source_id") in live_sources:
            protected_paths.update(image_paths(doc))
    released = set()
    for batch in pending:
        source_id = batch.document["request"]["source_ids"][0]
        if source_id in live_sources:
            continue
        own = sorted(
            (f for f in frames if f.source_id == source_id),
            key=lambda f: (f.document["source_seconds"], f.id),
        )
        # Evenly spaced exact frames give review context without retaining every survey sample.
        selected = {own[i].id for i in {0, len(own) // 2, len(own) - 1}} if own else set()
        keep, candidates = set(), set()
        for frame in own:
            candidates.update(image_paths(frame.document))
            if frame.id in selected:
                keep.update(image_paths(frame.document))
        own_ids = {f.id for f in own}
        for observation in observations:
            if observation.evidence_id in own_ids:
                candidates.update(image_paths(observation.document))
                if observation.evidence_id in selected:
                    keep.update(image_paths(observation.document))
        for path, doc in cached:
            if doc.get("source_id") == source_id:
                candidates.update(image_paths(doc))
                path.unlink(missing_ok=True)
        for name in candidates - keep - protected_paths:
            path = checked_path(root, name)
            if path.is_file():
                released.add(name)
                path.unlink()
        source = session.get(Source, source_id)
        retained = sorted(name for name in keep if checked_path(root, name).is_file())
        summary = {
            "policy": POLICY,
            "completed_at": now(),
            "review_evidence_ids": sorted(selected),
            "review_paths": retained,
            "routine_images_released": True,
        }
        source.document = {**source.document, "image_retention": summary}
        batch.document = {**batch.document, "image_cleanup": summary}
    # Old review frames are a cache, not a permanently growing archive.
    entries = [s for s in session.scalars(select(Source)) if s.document.get("image_retention")]
    entries.sort(key=lambda s: s.document["image_retention"]["completed_at"], reverse=True)
    retained_paths = set()
    retained_bytes = 0
    for source in entries:
        info = source.document["image_retention"]
        kept = []
        for name in info["review_paths"]:
            path = checked_path(root, name)
            if not path.is_file():
                continue
            size = path.stat().st_size
            if name in protected_paths or name in retained_paths or retained_bytes + size <= review_bytes:
                if name not in retained_paths:
                    retained_bytes += size
                retained_paths.add(name)
                kept.append(name)
            else:
                released.add(name)
                path.unlink()
        source.document = {**source.document, "image_retention": {**info, "review_paths": kept}}
    # Failed/crashed tasks can write images before publishing their Evidence/observation rows.
    # With all workers stopped, unreferenced writes cannot belong to an in-flight transaction.
    referenced = set().union(
        protected_paths,
        *(image_paths(f.document) for f in frames),
        *(image_paths(o.document) for o in observations),
        *(image_paths(doc) for _, doc in cached),
    )
    for path in (root / "evidence").rglob("*"):
        if path.is_file() and (
            path.suffix == ".tmp" or (path.suffix == ".png" and str(path.relative_to(root)) not in referenced)
        ):
            released.add(str(path.relative_to(root)))
            path.unlink()
    shared = _shared_cache_usage.get(str(root.resolve()))
    if shared is not None:
        with shared.get_lock():
            shared.value = cache_size(root)
    return {"released_files": len(released), "review_files": len(retained_paths)}
