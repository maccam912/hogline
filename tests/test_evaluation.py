import copy

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw
from sqlalchemy import select

from hogline.db import Evidence, LabelRevision
from hogline.domain.catalog import Conflict
from hogline.evaluation.datasets import freeze_dataset
from hogline.evaluation.labels import save_labels
from hogline.evaluation.metrics import evaluate_dataset, evaluate_view, match_points, summarize
from hogline.evaluation.models import DatasetCreate, EvaluationRequest, LabelEdit, LabelPoint, Labels
from hogline.media.frames import observe
from hogline.review.app import create_app


@pytest.fixture
def labeled_frame(db, sources):
    image = Image.new("RGB", (1920, 1080), (220, 220, 220))
    draw = ImageDraw.Draw(image)
    draw.ellipse((948, 788, 972, 812), fill=(210, 50, 40))
    draw.ellipse((1008, 838, 1032, 862), fill=(230, 210, 30))
    doc, _ = observe(
        image, {"pts": 0, "time_base_num": 1, "time_base_den": 30}, sources[0], db.settings.data_dir
    )
    with db.session() as session:
        row = Evidence(source_id=sources[0]["id"], evidence_key="synthetic-labelled-frame", document=doc)
        session.add(row)
        session.flush()
        evidence_id = row.id
    labels = Labels(
        evidence_id=evidence_id,
        view="house_bottom",
        evaluation_group="day-one",
        coverage="complete_visible",
        review_status="reviewed",
        provenance="synthetic",
        points=[
            LabelPoint(color="red", center_px=(240, 200)),
            LabelPoint(color="yellow", center_px=(300, 250)),
        ],
    )
    saved = save_labels(
        db,
        LabelEdit(
            expected_revision=0, author="Synthetic fixture", reason="Known image construction", labels=labels
        ),
    )
    return labels, saved


def freeze(db, **kwargs):
    return freeze_dataset(
        db,
        DatasetCreate(
            name="Frozen fixture",
            author="Fixture",
            reason="Regression",
            mode="exploratory",
            partitions={"day-one": "test"},
            **kwargs,
        ),
    )


def test_one_to_one_matching_handles_greedy_failure_and_color_mismatch():
    truth = [{"center_px": (0, 0), "color": "red"}, {"center_px": (4, 0), "color": "red"}]
    predictions = [{"center_px": (1.5, 0), "color": "red"}, {"center_px": (-3, 0), "color": "red"}]
    matches = match_points(truth, predictions, 100, 0.035)
    assert len(matches) == 2
    labels = {"points": [{"center_px": (20, 20), "color": "red"}], "ignore_regions": []}
    row = evaluate_view(labels, [{"center_px": (20, 20), "color": "yellow"}], 480, 0.035)
    assert row["true_positive"] == 1 and row["color_aware_true_positive"] == 0
    assert summarize([row])["color_accuracy_on_matched_known_colors"] == 0


def test_ignored_occlusions_and_empty_views_are_not_fake_perfect_recall():
    labels = {"points": [], "ignore_regions": [{"x": 0, "y": 0, "width": 30, "height": 30}]}
    result = evaluate_view(labels, [{"center_px": (20, 20), "color": "red"}], 480, 0.035)
    assert result["ignored_predictions"] == 1 and result["false_positive"] == 0
    summary = summarize([result])
    assert summary["precision"] is None and summary["recall"] is None and summary["f1"] is None
    assert summary["empty_views_correctly_empty"] == 1


def test_labels_validate_crop_bounds_ignore_regions_and_stale_revisions(db, labeled_frame):
    labels, saved = labeled_frame
    labels.note = "Revision two"
    save_labels(db, LabelEdit(expected_revision=1, author="Fixture", reason="Correction", labels=labels))
    with pytest.raises(Conflict):
        save_labels(db, LabelEdit(expected_revision=1, author="Fixture", reason="Stale", labels=labels))
    labels.points[0].center_px = (-1, 200)
    with pytest.raises(ValueError, match="outside"):
        save_labels(db, LabelEdit(expected_revision=2, author="Fixture", reason="Invalid", labels=labels))
    with db.session() as session:
        assert len(list(session.scalars(select(LabelRevision)))) == 2


def test_freeze_keeps_exact_label_revisions_after_corrections(db, labeled_frame):
    labels, _ = labeled_frame
    dataset = freeze(db)
    before = copy.deepcopy(dataset)
    labels.points = []
    save_labels(db, LabelEdit(expected_revision=1, author="Fixture", reason="Changed labels", labels=labels))
    report = evaluate_dataset(db, EvaluationRequest(dataset_id=dataset["id"]))
    assert dataset == before
    assert report["summary"]["true_positive"] == 2 and report["summary"]["false_negative"] == 0
    assert report["rows"][0]["label_revision"] == 1
    newer = freeze(db)
    report2 = evaluate_dataset(db, EvaluationRequest(dataset_id=newer["id"]))
    assert report2["summary"]["false_positive"] == 2


def test_confirmed_mode_excludes_nonhuman_and_partial_labels(db, labeled_frame):
    labels, _ = labeled_frame
    with pytest.raises(ValueError, match="No eligible"):
        freeze_dataset(
            db,
            DatasetCreate(name="Human-only", author="Fixture", reason="Test", partitions={"day-one": "test"}),
        )
    labels.provenance = "human"
    labels.coverage = "partial"
    save_labels(db, LabelEdit(expected_revision=1, author="Fixture", reason="Incomplete", labels=labels))
    with pytest.raises(ValueError, match="No eligible"):
        freeze(db)


def test_partition_leakage_is_rejected_between_views_of_one_recording(db, labeled_frame):
    labels, _ = labeled_frame
    second = labels.model_copy(update={"view": "house_top", "evaluation_group": "other-group", "points": []})
    save_labels(
        db, LabelEdit(expected_revision=0, author="Fixture", reason="Empty other view", labels=second)
    )
    with pytest.raises(ValueError, match="Split leakage"):
        freeze_dataset(
            db,
            DatasetCreate(
                name="Leaky",
                author="Fixture",
                reason="Must fail",
                mode="exploratory",
                partitions={"day-one": "train", "other-group": "test"},
            ),
        )


def test_missing_evidence_is_not_scored_as_detector_failure(db, labeled_frame):
    dataset = freeze(db)
    asset = dataset["records"][0]["evidence"]["crops"]["house_bottom"]
    (db.settings.data_dir / asset["path"]).write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="No reproducible"):
        evaluate_dataset(db, EvaluationRequest(dataset_id=dataset["id"]))


def test_label_and_evaluation_api_returns_frozen_downloads(db, labeled_frame):
    labels, saved = labeled_frame
    with TestClient(create_app(db.settings)) as client:
        target = f"/api/labels/{labels.evidence_id}/{labels.view}"
        assert client.get(target).json()["revision"] == 1
        body = LabelEdit(expected_revision=0, author="Fixture", reason="Stale", labels=labels).model_dump()
        assert client.put(target, json=body).status_code == 409
        response = client.post(
            "/api/evaluation/datasets",
            json={
                "name": "API fixture",
                "author": "Fixture",
                "reason": "API test",
                "mode": "exploratory",
                "partitions": {"day-one": "test"},
            },
        )
        dataset_id = response.json()["id"]
        response = client.post("/api/evaluation/runs", json={"dataset_id": dataset_id})
        assert response.status_code == 200
        report = response.json()
        assert report["summary"]["true_positive"] == 2
        assert client.get(f"/api/evaluation/runs/{report['id']}.json").json()["dataset_id"] == dataset_id
