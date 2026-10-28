import copy
import hashlib

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw
from sqlalchemy import select

from hogline.adapters.sources import digest
from hogline.db import EvaluationDataset, Evidence, HandleModel, HandleTrainingRun
from hogline.evaluation.datasets import freeze_dataset
from hogline.evaluation.labels import save_labels
from hogline.evaluation.metrics import evaluate_dataset
from hogline.evaluation.models import DatasetCreate, EvaluationRequest, LabelEdit, LabelPoint, Labels
from hogline.learning.handles import (
    HandleTrainingRequest,
    prepare_examples,
    registered_detector,
    train_handle_model,
    validate_training_separation,
)
from hogline.media.frames import observe
from hogline.perception.learned_handles import context_features, load_learned_detector
from hogline.review.app import create_app


@pytest.fixture
def training_dataset(db, sources):
    image = Image.new("RGB", (1920, 1080), (220, 220, 220))
    draw = ImageDraw.Draw(image)
    # Two handles on dark granite, two colored objects on white ice.
    for x, y, color, stone in [
        (948, 788, "red", True),
        (1008, 838, "yellow", True),
        (1068, 888, "red", False),
        (1128, 938, "yellow", False),
    ]:
        if stone:
            draw.ellipse((x - 8, y - 8, x + 32, y + 32), fill=(100, 100, 100))
        draw.ellipse((x, y, x + 24, y + 24), fill=(210, 50, 40) if color == "red" else (230, 210, 30))
    doc, _ = observe(
        image, {"pts": 0, "time_base_num": 1, "time_base_den": 30}, sources[0], db.settings.data_dir
    )
    with db.session() as session:
        row = Evidence(source_id=sources[0]["id"], evidence_key="training-fixture", document=doc)
        session.add(row)
        session.flush()
        evidence_id = row.id
    save_labels(
        db,
        LabelEdit(
            expected_revision=0,
            author="Fixture",
            reason="Known construction",
            labels=Labels(
                evidence_id=evidence_id,
                view="house_bottom",
                evaluation_group="train-day",
                coverage="complete_visible",
                review_status="reviewed",
                provenance="synthetic",
                points=[
                    LabelPoint(color="red", center_px=(240, 200)),
                    LabelPoint(color="yellow", center_px=(300, 250)),
                ],
            ),
        ),
    )
    return freeze_dataset(
        db,
        DatasetCreate(
            name="Training fixture",
            author="Fixture",
            reason="Regression",
            mode="exploratory",
            partitions={"train-day": "train"},
        ),
    )


def train(db, dataset):
    return train_handle_model(
        db, HandleTrainingRequest(dataset_id=dataset["id"], author="Fixture", reason="Test")
    )


def test_train_serialize_reload_and_evaluate_original_unaugmented_views(db, training_dataset):
    result = train(db, training_dataset)
    assert result["unique_class_counts"] == {"1": 2, "0": 2}
    assert result["augmented_examples"] == 8
    assert result["evaluation_scope"] == "in_sample_only"
    assert not result["model"]["auto_acceptance_authority"]
    assert result["model"]["activation"] == "experimental_benchmark_only"
    with db.snapshot_session() as session:
        detector, model = registered_detector(session, db.settings.data_dir, result["model_version"])
    assert detector.version == result["model_version"]
    report = evaluate_dataset(
        db,
        EvaluationRequest(
            dataset_id=training_dataset["id"], partition="train", model_version=detector.version
        ),
    )
    assert report["training_overlap_allowed_for_development"]
    assert report["learned_model_manifest_sha256"] == model["sha256"]
    assert report["summary"]["true_positive"] == 2
    assert report["summary"]["false_positive"] == report["summary"]["false_negative"] == 0
    client = TestClient(create_app(db.settings))
    assert client.get("/api/handle-models").json()["models"][0]["version"] == detector.version
    assert detector.version in client.get("/api/evaluation").json()["model_versions"]
    assert client.get(f"/api/handle-models/runs/{result['id']}.json").json()["augmented_examples"] == 8
    assert client.get("/api/handle-models/runs/missing.json").status_code == 404
    path = db.settings.data_dir / model["model"]["path"]
    path.write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="corrupt"):
        evaluate_dataset(
            db,
            EvaluationRequest(
                dataset_id=training_dataset["id"], partition="train", model_version=detector.version
            ),
        )


def test_training_day_cannot_be_rebranded_as_independent_test(db, training_dataset):
    result = train(db, training_dataset)
    frozen = copy.deepcopy(training_dataset)
    frozen.pop("id")
    frozen.pop("sha256")
    frozen["records"][0]["partition"] = "test"
    with db.session() as session:
        row = EvaluationDataset(sha256=digest(frozen), document=frozen)
        session.add(row)
        session.flush()
        identifier = row.id
    with pytest.raises(ValueError, match="Training leakage"):
        evaluate_dataset(
            db,
            EvaluationRequest(dataset_id=identifier, partition="test", model_version=result["model_version"]),
        )
    # Even another source ID cannot launder the shared day or label group.
    record = frozen["records"][0]
    record["source_manifest"]["id"] = "other-source"
    with pytest.raises(ValueError, match="Training leakage"):
        validate_training_separation(result["model"], [record], "validation")
    record["source_manifest"]["title_date"] = "different-day"
    record["labels"]["evaluation_group"] = "different-group"
    validate_training_separation(result["model"], [record], "test")


def test_examples_reject_missing_class_partial_review_cap_and_corruption(db, training_dataset):
    records = copy.deepcopy(training_dataset["records"])
    with pytest.raises(ValueError, match="limit"):
        prepare_examples(db.settings.data_dir, records, 6, lambda: None)
    records[0]["labels"]["points"] = []
    with pytest.raises(ValueError, match="needs reviewed handle examples"):
        prepare_examples(db.settings.data_dir, records, 100, lambda: None)
    records[0]["labels"]["provenance"] = "model_seeded"
    with pytest.raises(ValueError, match="independently reviewed"):
        prepare_examples(db.settings.data_dir, records, 100, lambda: None)
    asset = training_dataset["records"][0]["evidence"]["crops"]["house_bottom"]
    (db.settings.data_dir / asset["path"]).write_bytes(b"broken")
    with pytest.raises(ValueError, match="corrupt"):
        train(db, training_dataset)
    with db.snapshot_session() as session:
        assert session.scalar(select(HandleModel)) is None
        assert session.scalar(select(HandleTrainingRun)) is None


def test_manifest_integrity_features_and_directory_confinement(db, training_dataset):
    result = train(db, training_dataset)
    model = copy.deepcopy(result["model"])
    model["model"]["path"] = "../outside.xml"
    with pytest.raises(ValueError, match="outside"):
        load_learned_detector(db.settings.data_dir, model)
    rgb = np.zeros((40, 40, 3), dtype=np.uint8)
    assert context_features(rgb, (0, 0)).shape == (404,)
    assert np.isfinite(context_features(rgb, (39, 39), flip=True)).all()
    for center in [(float("nan"), 1), (40, 20), (-1, 1)]:
        with pytest.raises(ValueError, match="inside"):
            context_features(rgb, center)
    with db.session() as session:
        row = session.get(HandleModel, model["id"])
        row.document = {**row.document, "vote_threshold": 0.1}
    with pytest.raises(ValueError, match="checksum"):
        with db.snapshot_session() as session:
            registered_detector(session, db.settings.data_dir, model["version"])


def test_frozen_dataset_checksum_is_required_for_training(db, training_dataset):
    with db.session() as session:
        row = session.get(EvaluationDataset, training_dataset["id"])
        row.sha256 = hashlib.sha256(b"wrong").hexdigest()
    with pytest.raises(ValueError, match="corrupt"):
        train(db, training_dataset)
