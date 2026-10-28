"""Frozen, source/day-separated checks of traditional posted-card readings."""

import hashlib
import re
from collections import Counter, defaultdict
from typing import Literal

from pydantic import Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.orm.exc import StaleDataError

from hogline.adapters.sources import digest
from hogline.db import (
    BoardDataset,
    BoardEvaluation,
    BoardLabelRevision,
    BoardLabels,
    Evidence,
    FrameObservation,
    Source,
    now,
)
from hogline.domain.catalog import Conflict
from hogline.domain.models import Model
from hogline.evaluation.models import DatasetCreate


class BoardCard(Model):
    end_number: int = Field(ge=1, le=16, strict=True)
    cumulative_points: int = Field(ge=1, le=15, strict=True)


class BoardRowLabels(Model):
    visibility: Literal["readable", "unreadable"]
    cards: list[BoardCard] = Field(default_factory=list, max_length=15)

    @model_validator(mode="after")
    def complete_row(self):
        if self.visibility == "unreadable" and self.cards:
            raise ValueError("An unreadable row cannot supply complete card labels")
        if len({c.cumulative_points for c in self.cards}) != len(self.cards):
            raise ValueError("Label each point column at most once per row")
        self.cards.sort(key=lambda c: c.cumulative_points)
        return self


class BoardLabelEdit(Model):
    expected_revision: int = Field(ge=0)
    author: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=1, max_length=2000)
    provenance: Literal["human", "assistant_visual", "synthetic", "model_seeded"] = "human"
    review_status: Literal["draft", "reviewed"] = "draft"
    evaluation_group: str = Field(min_length=1, max_length=200)
    rows: dict[Literal["red", "yellow"], BoardRowLabels]
    note: str = Field(default="", max_length=5000)

    @model_validator(mode="after")
    def trustworthy_labels(self):
        if set(self.rows) != {"red", "yellow"}:
            raise ValueError("Label visibility for both red and yellow rows")
        if not all(value.strip() for value in (self.author, self.reason, self.evaluation_group)):
            raise ValueError("Reviewer, reason, and evaluation group cannot be blank")
        if self.provenance == "model_seeded" and self.review_status == "reviewed":
            raise ValueError("Model-seeded cards need independent inspection before review")
        return self


class BoardEvaluationRequest(Model):
    dataset_id: str
    model_version: str = Field(min_length=1, max_length=200)
    partition: Literal["train", "validation", "test"] = "test"


def verify_frame(root, frame):
    asset = frame["image"]
    path = (root / asset["path"]).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise ValueError("Original evidence image is unavailable; restore it before labeling or evaluating")
    if hashlib.sha256(path.read_bytes()).hexdigest() != asset["sha256"]:
        raise ValueError("Original evidence image checksum mismatch")


def save_board_labels(db, evidence_id, edit: BoardLabelEdit):
    with db.snapshot_session(write=True) as session:
        frame = session.get(Evidence, evidence_id)
        if frame is None:
            raise ValueError("Evidence frame not found")
        if frame.document.get("acquisition_tier") == "coarse":
            raise ValueError("Use an original-resolution frame for board labels")
        verify_frame(db.settings.data_dir, frame.document)
        row = session.get(BoardLabels, evidence_id)
        if (row.revision if row else 0) != edit.expected_revision:
            raise Conflict("Board labels changed; reload before saving")
        document = {
            "evidence_id": evidence_id,
            "source_id": frame.source_id,
            "image_sha256": frame.document["image"]["sha256"],
            **edit.model_dump(exclude={"expected_revision", "author", "reason"}),
        }
        if row is None:
            row = BoardLabels(id=evidence_id, document=document)
            session.add(row)
        else:
            row.document = document
            flag_modified(row, "document")
        try:
            session.flush()
        except StaleDataError as exc:
            raise Conflict("Board labels changed; reload before saving") from exc
        session.add(
            BoardLabelRevision(
                labels_id=row.id,
                number=row.revision,
                document=document,
                author=edit.author,
                reason=edit.reason,
            )
        )
        return {"id": row.id, "revision": row.revision, **document}


def freeze_board_dataset(db, request: DatasetCreate):
    if not request.partitions:
        raise ValueError("Assign at least one whole recording/day group")
    with db.snapshot_session(write=True) as session:
        records, exclusions = [], []
        source_partitions, day_partitions = {}, {}
        for labels in session.scalars(select(BoardLabels).order_by(BoardLabels.id)):
            label = labels.document
            group = label["evaluation_group"]
            if group not in request.partitions:
                continue
            frame = session.get(Evidence, labels.id)
            source = session.get(Source, frame.source_id).document
            partition = request.partitions[group]
            for registry, key in (
                (source_partitions, frame.source_id),
                (day_partitions, source.get("title_date") or frame.source_id),
            ):
                if registry.setdefault(key, partition) != partition:
                    raise ValueError("Split leakage: a recording or known recording day spans partitions")
            reason = None
            if label["review_status"] != "reviewed":
                reason = "not_reviewed"
            elif label["provenance"] == "model_seeded":
                reason = "unverified_model_seed"
            elif request.mode == "confirmed" and label["provenance"] != "human":
                reason = "nonhuman_labels_excluded"
            if reason:
                exclusions.append({"evidence_id": labels.id, "revision": labels.revision, "reason": reason})
                continue
            if frame.document["image"]["sha256"] != label["image_sha256"]:
                raise ValueError("Board labels no longer match their original evidence image")
            verify_frame(db.settings.data_dir, frame.document)
            records.append(
                {
                    "label_revision": labels.revision,
                    "labels": label,
                    "partition": partition,
                    "evidence": {**frame.document, "id": frame.id, "source_id": frame.source_id},
                    "source_manifest": source,
                }
            )
        missing = set(request.partitions) - {r["labels"]["evaluation_group"] for r in records}
        if missing:
            raise ValueError("Groups have no eligible reviewed board labels: " + ", ".join(sorted(missing)))
        document = {
            "schema_version": "board-evaluation-dataset-v1",
            **request.model_dump(),
            "created_at": now(),
            "records": records,
            "exclusions": exclusions,
            "notice": (
                "Posted cards, not end scores. "
                "Title-day groups need review for midnight games and duplicate uploads."
            ),
        }
        row = BoardDataset(sha256=digest(document), document=document)
        session.add(row)
        session.flush()
        return {"id": row.id, "sha256": row.sha256, **document}


def evaluate_board_row(label, board, color):
    predicted = [
        c for c in board.get("cards", []) if c.get("color") == color and c.get("end_number") is not None
    ]
    unreadable = any(c.get("color") == color for c in board.get("unreadable_slots", []))
    conflicts = any(c["end_number"] in board.get("conflicting_end_numbers", []) for c in predicted)
    row_read = board.get("status") == "observed" and not unreadable and not conflicts
    if label["visibility"] == "unreadable":
        return {
            "readable_rows": 0,
            "unreadable_rows": 1,
            "readable_predictions_on_unreadable_rows": int(row_read),
            "true_positive": 0,
            "false_positive": 0,
            "false_negative": 0,
            "exact_rows": 0,
            "abstained_readable_rows": 0,
        }
    truth = Counter((c["end_number"], c["cumulative_points"]) for c in label["cards"])
    prediction = (
        Counter((c["end_number"], c["cumulative_points"]) for c in predicted)
        if board.get("status") == "observed"
        else Counter()
    )
    tp = sum((truth & prediction).values())
    return {
        "readable_rows": 1,
        "unreadable_rows": 0,
        "readable_predictions_on_unreadable_rows": 0,
        "true_positive": tp,
        "false_positive": sum(prediction.values()) - tp,
        "false_negative": sum(truth.values()) - tp,
        "exact_rows": int(row_read and truth == prediction),
        "abstained_readable_rows": int(not row_read),
    }


def summarize_board_rows(rows):
    counts = Counter()
    for row in rows:
        counts.update(row["metrics"])
    tp, fp, fn = (counts[k] for k in ("true_positive", "false_positive", "false_negative"))
    return {
        **counts,
        "card_precision": tp / (tp + fp) if tp + fp else None,
        "card_recall": tp / (tp + fn) if tp + fn else None,
        "exact_row_accuracy": counts["exact_rows"] / counts["readable_rows"]
        if counts["readable_rows"]
        else None,
    }


def evaluate_board_dataset(db, request: BoardEvaluationRequest):
    with db.snapshot_session(write=True) as session:
        dataset = session.get(BoardDataset, request.dataset_id)
        if dataset is None or digest(dataset.document) != dataset.sha256:
            raise ValueError("Board dataset is missing or its checksum changed")
        records = [r for r in dataset.document["records"] if r["partition"] == request.partition]
        if not records:
            raise ValueError("No board frames in this partition")
        if not request.model_version.startswith("traditional-board-clock-"):
            raise ValueError("Choose a stored broadcast reader version")
        predictions = {
            r.evidence_id: r.document
            for r in session.scalars(
                select(FrameObservation).where(FrameObservation.model_version == request.model_version)
            )
        }
        rows, frozen_predictions = [], []
        missing = []
        for record in records:
            frame = record["evidence"]
            verify_frame(db.settings.data_dir, frame)
            current = session.get(Evidence, frame["id"])
            if current is None or current.document["image"]["sha256"] != frame["image"]["sha256"]:
                raise ValueError("Evaluation evidence differs from the frozen original image")
            observation = predictions.get(frame["id"])
            if observation and observation["source_id"] != frame["source_id"]:
                raise ValueError("Board prediction belongs to a different source")
            if observation:
                frozen_predictions.append(observation)
            else:
                missing.append(frame["id"])
            for color, label in record["labels"]["rows"].items():
                rows.append(
                    {
                        "evidence_id": frame["id"],
                        "source_id": frame["source_id"],
                        "source_seconds": frame["source_seconds"],
                        "youtube_url": frame.get("youtube_url"),
                        "sheet": record["source_manifest"].get("sheet"),
                        "color": color,
                        "label_revision": record["label_revision"],
                        "provenance": record["labels"]["provenance"],
                        "metrics": evaluate_board_row(
                            label, observation.get("scoreboard", {}) if observation else {}, color
                        ),
                    }
                )
        sheets = defaultdict(list)
        for row in rows:
            sheets[str(row["sheet"])].append(row)
        document = {
            "schema_version": "board-evaluation-run-v1",
            **request.model_dump(),
            "dataset_sha256": dataset.sha256,
            "created_at": now(),
            "mode": dataset.document["mode"],
            "frame_count": len(records),
            "rows": rows,
            "summary": summarize_board_rows(rows),
            "by_sheet": {k: summarize_board_rows(v) for k, v in sorted(sheets.items())},
            "provenances": sorted({r["provenance"] for r in rows}),
            "missing_prediction_evidence_ids": missing,
            "predictions": frozen_predictions,
            "notice": (
                "Missing reader outputs remain in the denominator. "
                "Unreadable labeled rows are reported separately. "
                "No end scores or game results are evaluated."
            ),
        }
        row = BoardEvaluation(dataset_id=dataset.id, sha256=digest(document), document=document)
        session.add(row)
        session.flush()
        return {"id": row.id, "sha256": row.sha256, **document}


def reader_order(version):
    match = re.search(r"-v(\d+)(?=:|$)", version)
    return (int(match.group(1)) if match else 0, version)


def board_evaluation_catalog(db):
    with db.session() as session:
        labels = [
            {"id": r.id, "revision": r.revision, **r.document} for r in session.scalars(select(BoardLabels))
        ]
        return {
            "labels": labels,
            "datasets": [
                {
                    "id": r.id,
                    "sha256": r.sha256,
                    "name": r.document["name"],
                    "partitions": r.document["partitions"],
                    "mode": r.document["mode"],
                    "record_count": len(r.document["records"]),
                }
                for r in session.scalars(select(BoardDataset))
            ],
            "runs": [
                {
                    "id": r.id,
                    "sha256": r.sha256,
                    **{k: v for k, v in r.document.items() if k not in ("predictions", "rows")},
                }
                for r in session.scalars(select(BoardEvaluation))
            ],
            "model_versions": sorted(
                set(
                    session.scalars(
                        select(FrameObservation.model_version).where(
                            FrameObservation.model_version.like("traditional-board-clock-%")
                        )
                    )
                ),
                key=reader_order,
                reverse=True,
            ),
        }
