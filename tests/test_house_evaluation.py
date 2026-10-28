import copy

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select

from hogline.adapters.sources import digest
from hogline.db import HouseDataset, HouseEvaluation, HouseLabelRevision, HouseLabels
from hogline.domain.catalog import catalog
from hogline.evaluation.house_states import (
    HouseEvaluationRequest,
    HouseLabelEdit,
    evaluate_house_dataset,
    freeze_house_dataset,
    save_house_labels,
)
from hogline.evaluation.models import DatasetCreate
from hogline.orchestration import windows, worker
from hogline.review.app import create_app
from tests.test_dense_analysis import fake_window  # noqa: F401


@pytest.fixture
def make_window(db, sources, fake_window, monkeypatch):  # noqa: F811
    decoder, inspector = windows.decode_window, windows.HSVHandleDetector.inspect

    def distinct_frames(*args, **kwargs):
        for i, (image, timing) in enumerate(decoder(*args, **kwargs)):
            image.putpixel((0, 0), (i, i, i))
            yield image, timing

    def without_fixture_speck(self, image):
        return {**inspector(self, image), "rejected_candidates": []}

    monkeypatch.setattr(windows, "decode_window", distinct_frames)
    monkeypatch.setattr(windows.HSVHandleDetector, "inspect", without_fixture_speck)

    def make(source=0, nonce=None):
        job = windows.enqueue_window(
            db,
            windows.WindowRequest(
                source_id=sources[source]["id"],
                start_seconds=100,
                end_seconds=105,
                run_nonce=nonce,
            ),
        )
        assert worker.run_one(db)["state"] == "succeeded"
        return next(w for w in catalog(db)["analysis_windows"] if w["job_id"] == job["id"])

    return make


def edit(**kw):
    return HouseLabelEdit.model_validate(
        {
            "expected_revision": 0,
            "author": "Synthetic test",
            "reason": "Known fixture",
            "evaluation_group": "day",
            "provenance": "synthetic",
            "review_status": "reviewed",
            "inspection_basis": "synthetic",
            "segments": [
                {
                    "first_sample": 0,
                    "last_sample": 9,
                    "state": "stable_handles",
                    "red_handles": 1,
                    "yellow_handles": 1,
                }
            ],
            **kw,
        }
    )


def freeze(db, **kw):
    return freeze_house_dataset(
        db,
        DatasetCreate.model_validate(
            {
                "name": "Synthetic houses",
                "author": "Test",
                "reason": "Regression",
                "mode": "exploratory",
                "partitions": {"day": "test"},
                **kw,
            }
        ),
    )


def evaluate(db, dataset):
    return evaluate_house_dataset(db, HouseEvaluationRequest(dataset_id=dataset["id"]))


def test_house_labels_api_preserves_revisions_original_timestamps_and_independence(db, make_window):
    window = make_window()
    client = TestClient(create_app(db.settings))
    path = f"/api/house-labels/{window['id']}/house_top"
    empty = client.get(path).json()
    assert empty["labels"] is None and len(empty["samples"]) == 10
    assert "detections" not in empty["samples"][0]  # Labels are not seeded from model output.
    assert empty["samples"][0]["pts"] == 100000
    first = client.put(path, json=edit().model_dump()).json()
    assert first["revision"] == 1
    assert first["segments"][0]["last"]["source_seconds"] == 104.5
    assert first["segments"][0]["first"]["youtube_url"].endswith("&t=100s")
    assert client.put(path, json=edit().model_dump()).status_code == 409
    second = client.put(path, json=edit(expected_revision=1, reason="Second review").model_dump()).json()
    assert second["revision"] == 2
    history = client.get(path).json()["history"]
    assert [h["revision"] for h in history] == [2, 1]
    assert history[0]["reason"] == "Second review" and history[1]["author"] == "Synthetic test"
    assert client.get(f"/api/house-labels/{window['id']}/perspective_left").status_code == 422
    assert client.get("/api/house-labels/missing/house_top").status_code == 404
    with pytest.raises(ValueError, match="past"):
        save_house_labels(
            db,
            window["id"],
            "house_top",
            edit(
                segments=[
                    {
                        "first_sample": 0,
                        "last_sample": 10,
                        "state": "unknown",
                    }
                ]
            ),
        )


def test_frozen_house_benchmark_keeps_revisions_counts_checksums_and_zero_denominators(db, make_window):
    window = make_window()
    first = save_house_labels(db, window["id"], "house_top", edit())
    dataset = freeze(db)
    assert dataset["records"][0]["label_revision"] == 1
    assert dataset["records"][0]["labels"]["input_parquet"] == window["parquet"]
    assert dataset["records"][0]["label_author"] == "Synthetic test"
    save_house_labels(
        db,
        window["id"],
        "house_top",
        edit(
            expected_revision=1,
            segments=[
                {
                    "first_sample": 0,
                    "last_sample": 9,
                    "state": "stable_handles",
                    "red_handles": 2,
                    "yellow_handles": 1,
                }
            ],
        ),
    )
    report = evaluate(db, dataset)
    summary = report["summary"]
    assert summary["evaluated_samples"] == summary["exact_stable_samples"] == 10
    assert summary["incorrect_stable_samples"] == summary["unsafe_stability_samples"] == 0
    assert summary["stable_prediction_precision"] == summary["stable_sample_recall"] == 1
    assert report["records"][0]["label_revision"] == first["revision"]
    assert report["by_provenance"]["synthetic"]["evaluated_samples"] == 10
    assert evaluate(db, freeze(db))["summary"]["incorrect_stable_samples"] == 10
    save_house_labels(
        db,
        window["id"],
        "house_top",
        edit(
            expected_revision=2,
            segments=[
                {
                    "first_sample": 0,
                    "last_sample": 9,
                    "state": "unknown",
                }
            ],
        ),
    )
    unknown = evaluate(db, freeze(db))["summary"]
    assert unknown["evaluated_samples"] == 0 and unknown["unlabeled_or_unknown_samples"] == 10
    assert unknown["stable_prediction_precision"] is unknown["stable_sample_recall"] is None
    client = TestClient(create_app(db.settings))
    assert client.get(f"/api/house-evaluation/datasets/{dataset['id']}.json").json() == dataset
    assert client.get(f"/api/house-evaluation/runs/{report['id']}.json").json() == report
    listing = client.get("/api/house-evaluation").json()
    assert len(listing["labels"]) == 1 and len(listing["datasets"]) == len(listing["runs"]) == 3


def test_abstentions_unsafe_stability_and_partial_label_coverage_have_separate_denominators(db, make_window):
    window = make_window()
    save_house_labels(
        db,
        window["id"],
        "house_top",
        edit(
            segments=[
                {
                    "first_sample": 0,
                    "last_sample": 3,
                    "state": "stable_handles",
                    "red_handles": 1,
                    "yellow_handles": 1,
                },
                {"first_sample": 4, "last_sample": 6, "state": "unobservable"},
            ]
        ),
    )
    top = evaluate(db, freeze(db))["summary"]
    assert top["evaluated_samples"] == 7 and top["unlabeled_or_unknown_samples"] == 3
    assert top["unsafe_stability_samples"] == 3 and top["exact_stable_samples"] == 4
    assert top["stable_prediction_precision"] == 4 / 7
    # The second overhead in this fixture has no usable ring, so it must abstain.
    save_house_labels(
        db,
        window["id"],
        "house_bottom",
        edit(
            segments=[
                {
                    "first_sample": 0,
                    "last_sample": 9,
                    "state": "stable_no_handles",
                    "red_handles": 0,
                    "yellow_handles": 0,
                }
            ]
        ),
    )
    report = evaluate(db, freeze(db))
    bottom = next(r for r in report["records"] if r["view"] == "house_bottom")
    assert bottom["stable_truth_samples"] == bottom["abstained_stable_truth_samples"] == 10
    assert bottom["stable_prediction_precision"] is None


def test_overlapping_windows_count_samples_once_and_conflicting_labels_cannot_freeze(db, make_window):
    first, second = make_window(), make_window(nonce="overlap")
    for window in (first, second):
        save_house_labels(db, window["id"], "house_top", edit())
    report = evaluate(db, freeze(db))
    assert report["summary"]["evaluated_samples"] == 10
    assert report["summary"]["duplicate_samples_excluded"] == 10
    assert sum(r["exact_stable_samples"] for r in report["records"]) == 10
    save_house_labels(
        db,
        second["id"],
        "house_top",
        edit(
            expected_revision=1,
            segments=[
                {
                    "first_sample": 0,
                    "last_sample": 9,
                    "state": "changing",
                }
            ],
        ),
    )
    with pytest.raises(ValueError, match="Overlapping"):
        freeze(db)


def test_confirmed_mode_anchor_only_and_group_leakage_guards(db, make_window):
    first, second = make_window(), make_window(source=1)
    save_house_labels(db, first["id"], "house_top", edit(evaluation_group="one"))
    with pytest.raises(ValueError, match="no eligible"):
        freeze(db, mode="confirmed", partitions={"one": "test"})
    save_house_labels(db, second["id"], "house_top", edit(evaluation_group="two"))
    with pytest.raises(ValueError, match="Split leakage"):
        freeze(db, partitions={"one": "train", "two": "test"})
    save_house_labels(
        db,
        first["id"],
        "house_top",
        edit(
            expected_revision=1,
            evaluation_group="one",
            provenance="human",
            inspection_basis="retained_frames_only",
        ),
    )
    with pytest.raises(ValueError, match="no eligible"):
        freeze(db, partitions={"one": "test"})
    save_house_labels(
        db,
        first["id"],
        "house_top",
        edit(
            expected_revision=2,
            evaluation_group="one",
            provenance="human",
            inspection_basis="source_interval",
        ),
    )
    assert freeze(db, mode="confirmed", partitions={"one": "test"})["mode"] == "confirmed"


@pytest.mark.parametrize(
    "change",
    [
        {"provenance": "model_seeded", "inspection_basis": "source_interval"},
        {"provenance": "human", "inspection_basis": "synthetic"},
        {"author": " "},
        {"segments": [{"first_sample": 0, "last_sample": 9, "state": "stable_handles"}]},
        {"segments": [{"first_sample": 0, "last_sample": 9, "state": "unobservable", "red_handles": 0}]},
        {"segments": [{"first_sample": 9, "last_sample": 0, "state": "unknown"}]},
        {
            "segments": [
                {"first_sample": 0, "last_sample": 9, "state": "unknown"},
                {"first_sample": 9, "last_sample": 9, "state": "unknown"},
            ]
        },
    ],
)
def test_label_contract_rejects_seeds_invented_counts_and_overlap(change):
    with pytest.raises(ValidationError):
        edit(**change)


def test_dataset_and_history_integrity_and_declared_limits(db, make_window, monkeypatch):
    from hogline.evaluation import house_states as module

    window = make_window()
    save_house_labels(db, window["id"], "house_top", edit())
    dataset = freeze(db)
    with pytest.raises(ValueError, match="no records"):
        evaluate_house_dataset(db, HouseEvaluationRequest(dataset_id=dataset["id"], partition="validation"))
    with pytest.raises(ValueError, match="unavailable"):
        evaluate_house_dataset(db, HouseEvaluationRequest(dataset_id=dataset["id"], model_version="old"))
    monkeypatch.setattr(module, "MAX_DATASET_BYTES", 1)
    with pytest.raises(ValueError, match="sixteen MiB"):
        freeze(db)
    with db.session() as session:
        history = session.scalar(select(HouseLabelRevision))
        history.document = {**history.document, "note": "changed"}
    with pytest.raises(ValueError, match="revision history"):
        freeze(db)
    with db.session() as session:
        row = session.get(HouseDataset, dataset["id"])
        doc = copy.deepcopy(row.document)
        doc["name"] = "changed"
        assert digest(doc) != row.sha256
        row.document = doc
        assert list(session.scalars(select(HouseLabels)))
        assert not list(session.scalars(select(HouseEvaluation)))
    with pytest.raises(ValueError, match="corrupt"):
        evaluate(db, dataset)
