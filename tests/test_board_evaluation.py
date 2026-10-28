import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select

from hogline.db import (
    BoardDataset,
    BoardEvaluation,
    BoardLabelRevision,
    BoardLabels,
    Evidence,
    FrameObservation,
    Source,
)
from hogline.domain.catalog import Conflict
from hogline.evaluation.boards import (
    BoardEvaluationRequest,
    BoardLabelEdit,
    evaluate_board_dataset,
    evaluate_board_row,
    freeze_board_dataset,
    save_board_labels,
)
from hogline.evaluation.models import DatasetCreate
from hogline.media.frames import observe
from hogline.review.app import create_app

VERSION = "traditional-board-clock-test"


def add_frame(db, source, seconds=0):
    doc, _ = observe(
        Image.new("RGB", (640, 360), "white"),
        {"pts": seconds, "time_base_num": 1, "time_base_den": 1},
        source,
        db.settings.data_dir,
    )
    with db.session() as session:
        row = Evidence(source_id=source["id"], evidence_key=doc["evidence_key"], document=doc)
        session.add(row)
        session.flush()
        return row.id


def edit(**kwargs):
    return BoardLabelEdit(
        expected_revision=kwargs.pop("expected_revision", 0),
        author="Reviewer",
        reason="Inspected source frame",
        evaluation_group=kwargs.pop("evaluation_group", "day-one"),
        review_status="reviewed",
        rows={
            "red": {"visibility": "readable", "cards": [{"end_number": 1, "cumulative_points": 2}]},
            "yellow": {"visibility": "unreadable", "cards": []},
        },
        **kwargs,
    )


def dataset_request(**kwargs):
    return DatasetCreate(
        name="Board check",
        author="Reviewer",
        reason="Freeze original labels",
        partitions=kwargs.pop("partitions", {"day-one": "test"}),
        **kwargs,
    )


def prediction(db, eid, source, cards=None):
    doc = {
        "id": f"prediction-{eid}",
        "evidence_id": eid,
        "source_id": source["id"],
        "model_version": VERSION,
        "scoreboard": {
            "status": "observed",
            "cards": cards
            if cards is not None
            else [{"color": "red", "end_number": 1, "cumulative_points": 2}],
            "unreadable_slots": [{"color": "yellow", "cumulative_points": 1}],
            "conflicting_end_numbers": [],
        },
    }
    with db.session() as session:
        session.add(FrameObservation(id=doc["id"], evidence_id=eid, model_version=VERSION, document=doc))


def test_label_revision_conflicts_history_and_frozen_dataset_are_independent(db, sources):
    eid = add_frame(db, sources[0])
    prediction(db, eid, sources[0])
    saved = save_board_labels(db, eid, edit())
    assert saved["revision"] == 1
    frozen = freeze_board_dataset(db, dataset_request())
    changed = edit(expected_revision=1)
    changed.rows["red"].cards[0].cumulative_points = 3
    assert save_board_labels(db, eid, changed)["revision"] == 2
    with pytest.raises(Conflict):
        save_board_labels(db, eid, changed)
    result = evaluate_board_dataset(
        db, BoardEvaluationRequest(dataset_id=frozen["id"], model_version=VERSION)
    )
    assert result["summary"]["true_positive"] == 1 and result["summary"]["exact_row_accuracy"] == 1
    assert result["rows"][0]["label_revision"] == 1
    with db.session() as session:
        history = list(session.scalars(select(BoardLabelRevision).order_by(BoardLabelRevision.number)))
        assert [r.number for r in history] == [1, 2]
        assert history[0].document["rows"]["red"]["cards"][0]["cumulative_points"] == 2
        row = session.get(FrameObservation, f"prediction-{eid}")
        row.document = {**row.document, "scoreboard": {"status": "unconfigured"}}
    with db.session() as session:
        assert (
            session.get(BoardEvaluation, result["id"]).document["predictions"][0]["scoreboard"]["status"]
            == "observed"
        )


def test_missing_predictions_count_misses_and_do_not_reward_empty_unprocessed_rows(db, sources):
    eid = add_frame(db, sources[0])
    save_board_labels(db, eid, edit())
    frozen = freeze_board_dataset(db, dataset_request())
    result = evaluate_board_dataset(
        db, BoardEvaluationRequest(dataset_id=frozen["id"], model_version=VERSION)
    )
    s = result["summary"]
    assert s["false_negative"] == 1 and s["exact_row_accuracy"] == 0
    assert s["abstained_readable_rows"] == 1 and s["card_precision"] is None
    assert result["missing_prediction_evidence_ids"] == [eid]
    assert evaluate_board_row({"visibility": "readable", "cards": []}, {}, "red")["exact_rows"] == 0
    assert (
        evaluate_board_row(
            {"visibility": "readable", "cards": []}, {"status": "observed", "cards": []}, "red"
        )["exact_rows"]
        == 1
    )


def test_card_errors_duplicates_conflicts_and_unreadable_rows_have_explicit_denominators():
    label = {"visibility": "readable", "cards": [{"end_number": 1, "cumulative_points": 2}]}
    board = {
        "status": "observed",
        "cards": [
            {"color": "red", "end_number": 1, "cumulative_points": 3},
            {"color": "red", "end_number": 1, "cumulative_points": 3},
        ],
        "conflicting_end_numbers": [1],
    }
    s = evaluate_board_row(label, board, "red")
    assert (s["true_positive"], s["false_positive"], s["false_negative"], s["exact_rows"]) == (0, 2, 1, 0)
    s = evaluate_board_row(
        {"visibility": "unreadable", "cards": []}, {"status": "observed", "cards": []}, "red"
    )
    assert s["readable_rows"] == 0 and s["readable_predictions_on_unreadable_rows"] == 1


def test_whole_day_leakage_is_rejected_even_across_sheets_and_groups(db, sources):
    for i, source in enumerate(sources):
        save_board_labels(db, add_frame(db, source), edit(evaluation_group=f"group-{i}"))
    with pytest.raises(ValueError, match="Split leakage"):
        freeze_board_dataset(db, dataset_request(partitions={"group-0": "train", "group-1": "test"}))
    with db.session() as session:
        row = session.get(Source, sources[1]["id"])
        row.document = {**row.document, "title_date": "2026-01-18", "sheet": 6}
    frozen = freeze_board_dataset(db, dataset_request(partitions={"group-0": "train", "group-1": "test"}))
    assert len(frozen["records"]) == 2
    result = evaluate_board_dataset(
        db, BoardEvaluationRequest(dataset_id=frozen["id"], model_version=VERSION)
    )
    assert result["frame_count"] == 1 and set(result["by_sheet"]) == {"6"}


def test_assistant_labels_are_exploratory_and_invalid_card_labels_rejected(db, sources):
    eid = add_frame(db, sources[0])
    save_board_labels(db, eid, edit(provenance="assistant_visual"))
    with pytest.raises(ValueError, match="no eligible"):
        freeze_board_dataset(db, dataset_request())
    assert (
        freeze_board_dataset(db, dataset_request(mode="exploratory"))["records"][0]["labels"]["provenance"]
        == "assistant_visual"
    )
    with pytest.raises(ValueError, match="independent inspection"):
        edit(provenance="model_seeded")
    for field, value in [("end_number", True), ("end_number", 17), ("cumulative_points", 0)]:
        raw = edit().model_dump()
        raw["rows"]["red"]["cards"][0][field] = value
        with pytest.raises(ValueError):
            BoardLabelEdit.model_validate(raw)
    raw = edit().model_dump()
    raw["rows"]["yellow"]["cards"] = raw["rows"]["red"]["cards"]
    with pytest.raises(ValueError, match="unreadable"):
        BoardLabelEdit.model_validate(raw)
    raw = edit().model_dump()
    raw["rows"]["red"]["cards"] *= 2
    with pytest.raises(ValueError, match="at most once"):
        BoardLabelEdit.model_validate(raw)


def test_checksum_changes_abort_labels_freeze_and_evaluation(db, sources):
    eid = add_frame(db, sources[0])
    save_board_labels(db, eid, edit())
    frozen = freeze_board_dataset(db, dataset_request())
    with db.session() as session:
        frame = session.get(Evidence, eid).document
    (db.settings.data_dir / frame["image"]["path"]).write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="checksum"):
        save_board_labels(db, eid, edit(expected_revision=1))
    with pytest.raises(ValueError, match="checksum"):
        freeze_board_dataset(db, dataset_request())
    with pytest.raises(ValueError, match="checksum"):
        evaluate_board_dataset(db, BoardEvaluationRequest(dataset_id=frozen["id"], model_version=VERSION))
    with db.session() as session:
        assert session.get(BoardLabels, eid).revision == 1
        assert not list(session.scalars(select(BoardEvaluation)))
        row = session.get(BoardDataset, frozen["id"])
        row.document = {**row.document, "name": "changed"}
    with pytest.raises(ValueError, match="checksum"):
        evaluate_board_dataset(db, BoardEvaluationRequest(dataset_id=frozen["id"], model_version=VERSION))


def test_board_api_saves_reads_freezes_evaluates_and_exports(db, sources):
    eid = add_frame(db, sources[0])
    prediction(db, eid, sources[0])
    client = TestClient(create_app(db.settings))
    assert client.get(f"/api/board-labels/{eid}").json() == {"labels": None, "history": []}
    assert client.put(f"/api/board-labels/{eid}", json=edit().model_dump()).status_code == 200
    assert client.put(f"/api/board-labels/{eid}", json=edit().model_dump()).status_code == 409
    assert len(client.get(f"/api/board-labels/{eid}").json()["history"]) == 1
    frozen = client.post("/api/board-evaluation/datasets", json=dataset_request().model_dump()).json()
    result = client.post(
        "/api/board-evaluation/runs", json={"dataset_id": frozen["id"], "model_version": VERSION}
    ).json()
    assert result["summary"]["true_positive"] == 1
    data = client.get("/api/board-evaluation").json()
    assert len(data["datasets"]) == len(data["runs"]) == len(data["labels"]) == 1
    assert client.get(f"/api/board-evaluation/runs/{result['id']}.json").json()["sha256"] == result["sha256"]
    assert client.get("/api/board-evaluation/runs/missing.json").status_code == 404
