"""Train a small reproducible handle-context classifier from frozen reviewed views."""

import hashlib
import os
import time
from collections import Counter
from uuid import uuid4

import cv2
import numpy as np
from PIL import Image
from pydantic import Field, model_validator
from sqlalchemy import select

from hogline.adapters.sources import digest, title_hints
from hogline.db import EvaluationDataset, HandleModel, HandleTrainingRun, MetadataSnapshot, now
from hogline.domain.models import Model
from hogline.evaluation.labels import inside
from hogline.perception.learned_handles import (
    FEATURE_VERSION,
    MAX_MODEL_BYTES,
    MODEL_FAMILY,
    classifier_votes,
    context_features,
    load_learned_detector,
)
from hogline.perception.stones import RawHSVHandleDetector


class HandleTrainingRequest(Model):
    dataset_id: str
    author: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=1, max_length=2000)
    seed: int = Field(default=17, ge=0, le=2147483647)
    trees: int = Field(default=96, ge=8, le=128)
    max_depth: int = Field(default=8, ge=2, le=10)
    vote_threshold: float = Field(default=0.8, gt=0, le=1)
    max_examples: int = Field(default=4096, ge=8, le=4096)
    max_worker_seconds: int = Field(default=120, ge=5, le=600)

    @model_validator(mode="after")
    def named_reason(self):
        if not self.author.strip() or not self.reason.strip():
            raise ValueError("Training reviewer and reason cannot be blank")
        return self


def image_for_record(root, record):
    asset = record["evidence"]["crops"][record["labels"]["view"]]
    path = (root / asset["path"]).resolve()
    if (
        not path.is_relative_to((root / "evidence").resolve())
        or not path.is_file()
        or path.stat().st_size > 16 * 1024**2
        or hashlib.sha256(path.read_bytes()).hexdigest() != asset["sha256"]
    ):
        raise ValueError("Training evidence is missing, corrupt or outside the evidence directory")
    image = np.asarray(Image.open(path).convert("RGB"))
    if image.shape[:2] != (asset["height"], asset["width"]):
        raise ValueError("Training image dimensions disagree with the frozen crop")
    return image


def record_identity(record):
    source = record["source_manifest"]
    return source["id"], source.get("title_date"), record["labels"]["evaluation_group"]


def known_days(session, source_ids):
    days = set()
    for row in session.scalars(select(MetadataSnapshot).where(MetadataSnapshot.source_id.in_(source_ids))):
        if digest(row.document) != row.sha256:
            raise ValueError("Source metadata checksum mismatch in the training lineage")
        day = title_hints(row.document.get("title") or "")["title_date"]
        if day:
            days.add(day)
    return days


def validate_training_separation(manifest, records, partition, session=None):
    if partition == "train":
        return
    identities = [record_identity(r) for r in records]
    days = {day for _, day, _ in identities if day}
    if session is not None:
        days.update(known_days(session, [sid for sid, _, _ in identities]))
    if (
        {sid for sid, _, _ in identities} & set(manifest["training_source_ids"])
        or days & set(manifest["training_title_days"])
        or {group for _, _, group in identities} & set(manifest["training_groups"])
    ):
        raise ValueError(
            "Training leakage: this validation/test cohort overlaps a trained recording, day or group"
        )


def prepare_examples(root, records, max_examples, tick):
    features, targets, provenance = [], [], []
    diagnostics = Counter()
    proposer = RawHSVHandleDetector()
    for record in records:
        tick()
        label = record["labels"]
        if (
            label["review_status"] != "reviewed"
            or label["coverage"] != "complete_visible"
            or label["provenance"] == "model_seeded"
        ):
            raise ValueError("Training requires independently reviewed complete visible frozen views")
        rgb = image_for_record(root, record)
        examples = [(p["center_px"], 1, "reviewed_handle_center") for p in label["points"]]
        proposals = proposer.detect(rgb)
        for candidate in proposals:
            point = candidate["center_px"]
            negative = any(inside(point, r) for r in label.get("negative_regions", []))
            if not negative and any(inside(point, r) for r in label["ignore_regions"]):
                diagnostics["ignored_proposals"] += 1
                continue
            if not negative and any(
                np.linalg.norm(np.asarray(point) - p["center_px"]) <= max(3, rgb.shape[1] * 0.02)
                for p in label["points"]
            ):
                diagnostics["proposals_near_labeled_handles"] += 1
                continue
            examples.append(
                (point, 0, "known_nonstone_region" if negative else "unmatched_complete_view_proposal")
            )
        for center, target, kind in examples:
            tick()
            if len(features) + 2 > max_examples:
                raise ValueError(
                    "Training example limit reached; freeze a smaller cohort or raise the declared cap"
                )
            for flip in (False, True):
                features.append(context_features(rgb, center, flip=flip))
                targets.append(target)
            provenance.append(
                {
                    "labels_id": record["labels_id"],
                    "label_revision": record["label_revision"],
                    "evidence_id": label["evidence_id"],
                    "view": label["view"],
                    "center_px": list(center),
                    "target": target,
                    "label_basis": kind,
                    "crop_sha256": record["evidence"]["crops"][label["view"]]["sha256"],
                }
            )
    if set(targets) != {0, 1}:
        raise ValueError("Training needs reviewed handle examples and known non-handle proposals")
    return (
        np.stack(features).astype(np.float32),
        np.asarray(targets, dtype=np.int32),
        provenance,
        dict(diagnostics),
    )


def checked_model(row):
    if digest(row.document) != row.sha256:
        raise ValueError("Learned handle model manifest checksum mismatch")
    return {"id": row.id, "sha256": row.sha256, **row.document}


def registered_detector(session, root, version):
    row = session.scalar(select(HandleModel).where(HandleModel.version == version))
    if row is None:
        return None, None
    manifest = checked_model(row)
    return load_learned_detector(root, manifest), manifest


def train_handle_model(db, request: HandleTrainingRequest):
    started = time.monotonic()

    def tick():
        if time.monotonic() - started > request.max_worker_seconds:
            raise ValueError("Handle-model training worker-time budget reached")

    with db.snapshot_session() as session:
        dataset = session.get(EvaluationDataset, request.dataset_id)
        if dataset is None or digest(dataset.document) != dataset.sha256:
            raise ValueError("Frozen training dataset is missing or corrupt")
        frozen, dataset_sha = dataset.document, dataset.sha256
        records = [r for r in frozen["records"] if r["partition"] == "train"]
        if not records or len(records) > 256:
            raise ValueError("Training requires one to 256 frozen train-partition views")
        identities = [record_identity(r) for r in records]
        source_ids = sorted({sid for sid, _, _ in identities})
        days = sorted({day for _, day, _ in identities if day} | known_days(session, source_ids))
        validation_records = [r for r in frozen["records"] if r["partition"] != "train"]
        training_scope = {
            "training_source_ids": source_ids,
            "training_title_days": days,
            "training_groups": sorted({group for _, _, group in identities}),
        }
        validate_training_separation(training_scope, validation_records, "validation", session)
    x, y, examples, diagnostics = prepare_examples(db.settings.data_dir, records, request.max_examples, tick)
    model = cv2.ml.RTrees_create()
    model.setMaxDepth(request.max_depth)
    model.setMinSampleCount(2)
    model.setMaxCategories(2)
    model.setCalculateVarImportance(True)
    model.setActiveVarCount(0)
    model.setTermCriteria((cv2.TERM_CRITERIA_MAX_ITER, request.trees, 0))
    cv2.setRNGSeed(request.seed)
    if not model.train(x, cv2.ml.ROW_SAMPLE, y):
        raise ValueError("OpenCV did not train the handle classifier")
    tick()
    scores = classifier_votes(model, x)
    predicted = scores >= request.vote_threshold
    training_counts = {
        "true_positive": int(np.sum(predicted & (y == 1))),
        "false_positive": int(np.sum(predicted & (y == 0))),
        "false_negative": int(np.sum(~predicted & (y == 1))),
        "true_negative": int(np.sum(~predicted & (y == 0))),
    }
    directory = db.settings.data_dir / "models/trained-handles"
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / f".{uuid4()}.xml"
    try:
        model.save(str(temporary))
        if not 0 < temporary.stat().st_size <= MAX_MODEL_BYTES:
            raise ValueError("Learned handle weights exceeded the sixteen-MiB model limit")
        content = temporary.read_bytes()
        weights_sha = hashlib.sha256(content).hexdigest()
        path = directory / f"{weights_sha}.xml"
        if path.exists():
            if hashlib.sha256(path.read_bytes()).hexdigest() != weights_sha:
                raise ValueError("Existing learned handle weights are corrupt")
        else:
            os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    tick()
    identity = digest(
        {
            "weights": weights_sha,
            "threshold": request.vote_threshold,
            "dataset": dataset_sha,
            "features": FEATURE_VERSION,
        }
    )
    version = f"{MODEL_FAMILY}:{identity[:16]}"
    manifest = {
        "schema_version": "hogline-handle-model-v1",
        "version": version,
        "created_at": now(),
        "feature_version": FEATURE_VERSION,
        "raw_proposer_version": RawHSVHandleDetector.version,
        "vote_threshold": request.vote_threshold,
        "opencv_version": cv2.__version__,
        "training_dataset_id": request.dataset_id,
        "training_dataset_sha256": dataset_sha,
        **training_scope,
        "training_label_provenance": dict(Counter(r["labels"]["provenance"] for r in records)),
        "model": {
            "path": str(path.relative_to(db.settings.data_dir)),
            "sha256": weights_sha,
            "bytes": len(content),
        },
        "activation": "experimental_benchmark_only",
        "auto_acceptance_authority": False,
    }
    document = {
        "schema_version": "hogline-handle-training-run-v1",
        **request.model_dump(),
        "created_at": now(),
        "dataset_sha256": dataset_sha,
        "feature_version": FEATURE_VERSION,
        "training_views": len(records),
        "unique_examples": len(examples),
        "augmented_examples": len(y),
        "augmentation": "original_and_horizontal_flip",
        "unique_class_counts": dict(Counter(str(e["target"]) for e in examples)),
        "feature_matrix_sha256": hashlib.sha256(x.tobytes() + y.tobytes()).hexdigest(),
        "examples": examples,
        "diagnostics": diagnostics,
        "in_sample_counts": training_counts,
        "evaluation_scope": "held_out_partitions_available" if validation_records else "in_sample_only",
        "wall_seconds": time.monotonic() - started,
        "notice": (
            "Context classifier for raw colored-handle proposals, not a trained granite-footprint detector. "
            "Training fit is not independent accuracy. Votes are uncalibrated; frozen validation/test days "
            "must be disjoint from training. No automatic score, delivery or identity acceptance is enabled."
        ),
    }
    # Exercise the serialized form before registering it as a reproducible model.
    load_learned_detector(db.settings.data_dir, manifest)
    tick()
    with db.snapshot_session(write=True) as session:
        existing = session.scalar(select(HandleModel).where(HandleModel.version == version))
        if existing is None:
            existing = HandleModel(version=version, sha256=digest(manifest), document=manifest)
            session.add(existing)
            session.flush()
        else:
            checked_model(existing)
        document["model_id"], document["model_version"] = existing.id, version
        run = HandleTrainingRun(
            dataset_id=request.dataset_id, model_id=existing.id, sha256=digest(document), document=document
        )
        session.add(run)
        session.flush()
        return {"id": run.id, "sha256": run.sha256, **document, "model": checked_model(existing)}
