"""Independent interval labels and frozen checks of sampled house configurations."""

import json
from collections import Counter, defaultdict
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

from pydantic import Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.orm.exc import StaleDataError

from hogline.adapters.sources import digest, title_hints
from hogline.analytics.house_states import checked_result, save_house_states_in_session
from hogline.analytics.tracks import load_track_input
from hogline.db import (
    AnalysisWindow,
    HouseDataset,
    HouseEvaluation,
    HouseLabelRevision,
    HouseLabels,
    MetadataSnapshot,
    Source,
    WindowHouseStates,
    now,
)
from hogline.domain.catalog import Conflict
from hogline.domain.models import Model
from hogline.evaluation.models import DatasetCreate
from hogline.reconstruction.house_states import VERSION, anchor

MAX_RECORDS = 128
MAX_DATASET_BYTES = 16 * 1024**2
View = Literal["house_top", "house_bottom"]
COUNT_FIELDS = (
    "evaluated_samples",
    "stable_truth_samples",
    "predicted_stable_samples",
    "exact_stable_samples",
    "incorrect_stable_samples",
    "unsafe_stability_samples",
    "abstained_samples",
    "abstained_stable_truth_samples",
    "unlabeled_or_unknown_samples",
    "duplicate_samples_excluded",
)


class HouseSegment(Model):
    first_sample: int = Field(ge=0, le=239, strict=True)
    last_sample: int = Field(ge=0, le=239, strict=True)
    state: Literal["stable_handles", "stable_no_handles", "changing", "unobservable", "unknown"]
    red_handles: int | None = Field(default=None, ge=0, le=128, strict=True)
    yellow_handles: int | None = Field(default=None, ge=0, le=128, strict=True)

    @model_validator(mode="after")
    def meaningful(self):
        if self.last_sample < self.first_sample:
            raise ValueError("A label interval needs ordered inclusive sample indices")
        if self.state in ("stable_handles", "stable_no_handles"):
            if self.red_handles is None or self.yellow_handles is None:
                raise ValueError("Stable labels require independently inspected counts for both colors")
            if (self.red_handles + self.yellow_handles == 0) != (self.state == "stable_no_handles"):
                raise ValueError("Use stable_no_handles only for zero visible handles")
        elif self.red_handles is not None or self.yellow_handles is not None:
            raise ValueError("Changing, unobservable and unknown intervals have null stable counts")
        return self


class HouseLabelEdit(Model):
    expected_revision: int = Field(ge=0)
    author: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=1, max_length=2000)
    evaluation_group: str = Field(min_length=1, max_length=200)
    provenance: Literal["human", "assistant_visual", "synthetic", "model_seeded"] = "human"
    review_status: Literal["draft", "reviewed"] = "draft"
    inspection_basis: Literal["source_interval", "retained_frames_only", "synthetic"]
    segments: list[HouseSegment] = Field(min_length=1, max_length=240)
    note: str = Field(default="", max_length=5000)

    @model_validator(mode="after")
    def independent(self):
        if not all(s.strip() for s in (self.author, self.reason, self.evaluation_group)):
            raise ValueError("Reviewer, reason and evaluation group cannot be blank")
        if self.provenance == "model_seeded" and self.review_status == "reviewed":
            raise ValueError("Model seeds cannot become reviewed house-state ground truth")
        if (self.provenance == "synthetic") != (self.inspection_basis == "synthetic"):
            raise ValueError("Synthetic inspection must declare synthetic label provenance")
        self.segments.sort(key=lambda s: s.first_sample)
        if any(
            a.last_sample >= b.first_sample for a, b in zip(self.segments, self.segments[1:], strict=False)
        ):
            raise ValueError("House label intervals cannot overlap within a view")
        return self


class HouseEvaluationRequest(Model):
    dataset_id: str
    model_version: str = Field(default=VERSION, min_length=1, max_length=200)
    partition: Literal["train", "validation", "test"] = "test"


def label_id(window_id, view):
    return str(uuid5(NAMESPACE_URL, f"house-labels:{window_id}:{view}"))


def save_house_labels(db, window_id, view: View, edit: HouseLabelEdit):
    if view not in ("house_top", "house_bottom"):
        raise ValueError("Choose an overhead house view")
    with db.snapshot_session(write=True) as session:
        window_row = session.get(AnalysisWindow, window_id)
        if window_row is None:
            raise ValueError("Detailed window not found")
        window = {**window_row.document, "id": window_id, "source_id": window_row.source_id}
        rows = [r for r in load_track_input(db.settings.data_dir, window) if r["view"] == view]
        by_index = {r["sample_index"]: r for r in rows}
        segments = []
        for segment in edit.segments:
            if segment.last_sample >= window["sample_count"]:
                raise ValueError("House label interval extends past this window's original samples")
            segments.append(
                {
                    **segment.model_dump(),
                    "first": anchor(by_index[segment.first_sample]),
                    "last": anchor(by_index[segment.last_sample]),
                }
            )
        identifier = label_id(window_id, view)
        row = session.get(HouseLabels, identifier)
        if (row.revision if row else 0) != edit.expected_revision:
            raise Conflict("House labels changed; reload before saving")
        document = {
            **edit.model_dump(exclude={"expected_revision", "author", "reason", "segments"}),
            "window_id": window_id,
            "view": view,
            "source_id": window["source_id"],
            "source_metadata_sha256": window["source_metadata_sha256"],
            "input_parquet": window["parquet"],
            "segments": segments,
        }
        if row is None:
            row = HouseLabels(id=identifier, window_id=window_id, view=view, document=document)
            session.add(row)
        else:
            row.document = document
            flag_modified(row, "document")
        try:
            session.flush()
        except StaleDataError as exc:
            raise Conflict("House labels changed; reload before saving") from exc
        session.add(
            HouseLabelRevision(
                labels_id=identifier,
                number=row.revision,
                document=document,
                author=edit.author,
                reason=edit.reason,
            )
        )
        return {"id": row.id, "revision": row.revision, **document}


def truth_at(segments, sample_index):
    return next((s for s in segments if s["first_sample"] <= sample_index <= s["last_sample"]), None)


def sample_key(record, row):
    return (
        record["window"]["source_id"],
        record["window"]["source_metadata_sha256"],
        record["labels"]["view"],
        round(row["source_seconds"], 6),
    )


def validate_label_input(label, window, rows, view):
    if (
        label["window_id"] != window["id"]
        or label["view"] != view
        or label["source_id"] != window["source_id"]
        or label["source_metadata_sha256"] != window["source_metadata_sha256"]
        or label["input_parquet"] != window["parquet"]
    ):
        raise ValueError("House labels no longer match their dense observations")
    by_index = {r["sample_index"]: r for r in rows if r["view"] == view}
    for segment in label["segments"]:
        if (
            segment["first_sample"] not in by_index
            or segment["last_sample"] not in by_index
            or segment["first"] != anchor(by_index[segment["first_sample"]])
            or segment["last"] != anchor(by_index[segment["last_sample"]])
        ):
            raise ValueError("House labels no longer match their original sample timestamps")


def freeze_house_dataset(db, request: DatasetCreate):
    if not request.partitions:
        raise ValueError("Assign at least one whole recording/day group")
    with db.snapshot_session(write=True) as session:
        records, exclusions, partitions, truth_seen = [], [], {}, {}
        for row in session.scalars(select(HouseLabels).order_by(HouseLabels.id)):
            label = row.document
            group = label["evaluation_group"]
            if group not in request.partitions:
                continue
            if len(records) >= MAX_RECORDS:
                raise ValueError("House benchmark exceeds 128 window/view records; freeze a smaller cohort")
            window_row = session.get(AnalysisWindow, row.window_id)
            window = {**window_row.document, "id": window_row.id, "source_id": window_row.source_id}
            source = session.get(Source, window_row.source_id).document
            metadata = session.scalar(
                select(MetadataSnapshot).where(
                    MetadataSnapshot.source_id == window_row.source_id,
                    MetadataSnapshot.sha256 == window["source_metadata_sha256"],
                )
            )
            if metadata is None or digest(metadata.document) != metadata.sha256:
                raise ValueError("Pinned source metadata is missing or corrupt")
            pinned_day = title_hints(metadata.document.get("title") or "")["title_date"]
            partition = request.partitions[group]
            for key in (
                ("source", window_row.source_id),
                ("day", pinned_day or window_row.source_id),
                ("day", source.get("title_date") or window_row.source_id),
            ):
                if partitions.setdefault(key, partition) != partition:
                    raise ValueError("Split leakage: a recording or known recording day spans partitions")
            reason = (
                "not_reviewed"
                if label["review_status"] != "reviewed"
                else "retained_frames_do_not_label_an_interval"
                if label["inspection_basis"] == "retained_frames_only"
                else "unverified_model_seed"
                if label["provenance"] == "model_seeded"
                else "nonhuman_labels_excluded"
                if request.mode == "confirmed" and label["provenance"] != "human"
                else None
            )
            if reason:
                exclusions.append({"labels_id": row.id, "revision": row.revision, "reason": reason})
                continue
            if label["input_parquet"] != window["parquet"]:
                raise ValueError("House labels no longer match their dense observations")
            revision = session.scalar(
                select(HouseLabelRevision).where(
                    HouseLabelRevision.labels_id == row.id,
                    HouseLabelRevision.number == row.revision,
                )
            )
            if revision is None or revision.document != label:
                raise ValueError("House labels disagree with their revision history")
            samples = [r for r in load_track_input(db.settings.data_dir, window) if r["view"] == row.view]
            validate_label_input(label, window, samples, row.view)
            record = {
                "labels_id": row.id,
                "label_revision": row.revision,
                "labels": label,
                "label_author": revision.author,
                "label_reason": revision.reason,
                "partition": partition,
                "window": window,
                "source_metadata": metadata.document,
                "source_metadata_sha256": metadata.sha256,
                "sheet": title_hints(metadata.document.get("title") or "")["sheet"],
            }
            for sample in samples:
                truth = truth_at(label["segments"], sample["sample_index"])
                if truth is None or truth["state"] == "unknown":
                    continue
                value = (truth["state"], truth["red_handles"], truth["yellow_handles"])
                if truth_seen.setdefault(sample_key(record, sample), value) != value:
                    raise ValueError("Overlapping window labels disagree at the same source time and view")
            records.append(record)
        missing = set(request.partitions) - {r["labels"]["evaluation_group"] for r in records}
        if missing:
            raise ValueError("Groups have no eligible interval labels: " + ", ".join(sorted(missing)))
        document = {
            "schema_version": "house-evaluation-dataset-v1",
            **request.model_dump(),
            "created_at": now(),
            "records": records,
            "exclusions": exclusions,
            "notice": (
                "Whole recording/title-day groups stay together. "
                "Confirm midnight and duplicate families manually."
            ),
        }
        if len(json.dumps(document).encode()) > MAX_DATASET_BYTES:
            raise ValueError("House benchmark exceeds sixteen MiB; freeze a smaller cohort")
        row = HouseDataset(sha256=digest(document), document=document)
        session.add(row)
        session.flush()
        return {"id": row.id, "sha256": row.sha256, **document}


def evaluate_samples(record, samples, result, seen):
    predictions = {
        index: s
        for s in result["states"]
        if s["view"] == record["labels"]["view"]
        for index in s["sample_indices"]
    }
    counts, confusion = Counter(), Counter()
    for sample in samples:
        truth = truth_at(record["labels"]["segments"], sample["sample_index"])
        if truth is None or truth["state"] == "unknown":
            counts["unlabeled_or_unknown_samples"] += 1
            continue
        key = sample_key(record, sample)
        if key in seen:
            counts["duplicate_samples_excluded"] += 1
            continue
        seen.add(key)
        counts["evaluated_samples"] += 1
        prediction = predictions.get(sample["sample_index"])
        predicted_class = (
            "stable_handles"
            if prediction and prediction["candidate_count"]
            else "stable_no_handles"
            if prediction
            else "abstain"
        )
        confusion[(truth["state"], predicted_class)] += 1
        stable_truth = truth["state"] in ("stable_handles", "stable_no_handles")
        counts["stable_truth_samples"] += stable_truth
        if prediction:
            counts["predicted_stable_samples"] += 1
            exact = stable_truth and all(
                prediction["color_counts"].get(color, 0) == truth[f"{color}_handles"]
                for color in ("red", "yellow")
            )
            counts["exact_stable_samples"] += bool(exact)
            counts["incorrect_stable_samples"] += not exact
            counts["unsafe_stability_samples"] += not stable_truth
        else:
            counts["abstained_samples"] += 1
            counts["abstained_stable_truth_samples"] += stable_truth
    return counts, confusion


def summarize(counts):
    def ratio(n, d):
        return counts[n] / counts[d] if counts[d] else None

    return {
        **{key: counts[key] for key in COUNT_FIELDS},
        "stable_prediction_precision": ratio("exact_stable_samples", "predicted_stable_samples"),
        "stable_sample_recall": ratio("exact_stable_samples", "stable_truth_samples"),
        "prediction_coverage": ratio("predicted_stable_samples", "evaluated_samples"),
    }


def evaluate_house_dataset(db, request: HouseEvaluationRequest):
    with db.snapshot_session(write=True) as session:
        dataset = session.get(HouseDataset, request.dataset_id)
        if dataset is None or digest(dataset.document) != dataset.sha256:
            raise ValueError("House dataset is missing or corrupt")
        records = [r for r in dataset.document["records"] if r["partition"] == request.partition]
        if not records:
            raise ValueError("This house dataset has no records in the requested partition")
        total, confusion, by_sheet, by_provenance = (
            Counter(),
            Counter(),
            defaultdict(Counter),
            defaultdict(Counter),
        )
        reports, seen = [], set()
        for record in records:
            window = record["window"]
            current = session.get(AnalysisWindow, window["id"])
            if (
                current is None
                or {**current.document, "id": current.id, "source_id": current.source_id} != window
            ):
                raise ValueError("Frozen window no longer matches its immutable source record")
            rows = load_track_input(db.settings.data_dir, window)
            validate_label_input(record["labels"], window, rows, record["labels"]["view"])
            if request.model_version == VERSION:
                result = save_house_states_in_session(session, window, rows)
            else:
                stored = session.scalar(
                    select(WindowHouseStates).where(
                        WindowHouseStates.window_id == window["id"],
                        WindowHouseStates.version == request.model_version,
                    )
                )
                if stored is None:
                    raise ValueError("Requested house-state version is unavailable for a frozen window")
                result = checked_result(stored)
            if (
                result["status"] != "derived"
                or result["version"] != request.model_version
                or any(
                    result[key] != window[other]
                    for key, other in (
                        ("source_id", "source_id"),
                        ("source_metadata_sha256", "source_metadata_sha256"),
                        ("input_parquet", "parquet"),
                        ("profile_sha256", "profile_sha256"),
                        ("detector_version", "model_version"),
                        ("visibility_version", "visibility_version"),
                    )
                )
            ):
                raise ValueError("House result is unavailable or disagrees with the frozen dense input")
            samples = [r for r in rows if r["view"] == record["labels"]["view"]]
            counts, matrix = evaluate_samples(record, samples, result, seen)
            total.update(counts)
            confusion.update(matrix)
            by_sheet[str(record["sheet"] or "unknown")].update(counts)
            by_provenance[record["labels"]["provenance"]].update(counts)
            reports.append(
                {
                    "labels_id": record["labels_id"],
                    "label_revision": record["label_revision"],
                    "window_id": window["id"],
                    "view": record["labels"]["view"],
                    "result_id": result["id"],
                    "result_sha256": result["sha256"],
                    **summarize(counts),
                }
            )
        document = {
            "schema_version": "house-evaluation-run-v1",
            **request.model_dump(),
            "created_at": now(),
            "dataset_sha256": dataset.sha256,
            "summary": summarize(total),
            "records": reports,
            "confusion": [
                {"truth": truth, "prediction": prediction, "samples": count}
                for (truth, prediction), count in sorted(confusion.items())
            ],
            "by_sheet": {k: summarize(v) for k, v in sorted(by_sheet.items())},
            "by_provenance": {k: summarize(v) for k, v in sorted(by_provenance.items())},
            "notice": (
                "Sample-level descriptive checks, not independent trials or an archive-wide accuracy claim. "
                "Exact stable agreement requires both visible handle counts; it does not validate positions, "
                "granite footprints, empty ice, played-end boundaries or scoring. "
                "Overlapping source-time/view samples count once; unknown and unlabeled samples are excluded."
            ),
        }
        row = HouseEvaluation(dataset_id=dataset.id, sha256=digest(document), document=document)
        session.add(row)
        session.flush()
        return {"id": row.id, "sha256": row.sha256, **document}
