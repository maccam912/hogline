import hashlib
import math
from uuid import NAMESPACE_URL, uuid5

import numpy as np
from scipy.optimize import linear_sum_assignment
from sqlalchemy import select

from hogline.adapters.sources import digest
from hogline.db import EvaluationDataset, EvaluationRun, Evidence, FrameObservation, now
from hogline.evaluation.labels import inside
from hogline.evaluation.models import EvaluationRequest
from hogline.perception.pipeline import analyze_frame
from hogline.perception.stones import VERSION


def match_points(truth, predictions, width, tolerance, *, color_aware=False):
    """Maximum-cardinality one-to-one matching, then minimum center error.

    Dummy assignments cost one each; all real-pair costs combined stay below one.
    This avoids greedy matching losing a valid pair in a clustered house.
    """
    n, m = len(truth), len(predictions)
    if not n or not m:
        return []
    cost = np.ones((n, m + n))
    cost[:, :m] = 1000.0
    distances = np.zeros((n, m))
    for i, label in enumerate(truth):
        for j, detection in enumerate(predictions):
            distance = math.dist(label["center_px"], detection["center_px"]) / width
            distances[i, j] = distance
            color_ok = not color_aware or label["color"] == "unknown" or label["color"] == detection["color"]
            if distance <= tolerance and color_ok:
                cost[i, j] = distance / tolerance / (n + 1)
    rows, columns = linear_sum_assignment(cost)
    return [
        {"truth_index": int(i), "prediction_index": int(j), "error_fraction": float(distances[i, j])}
        for i, j in zip(rows, columns, strict=True)
        if i < n and j < m and cost[i, j] < 1
    ]


def evaluate_view(labels, predictions, width, tolerance):
    ignored = [d for d in predictions if any(inside(d["center_px"], r) for r in labels["ignore_regions"])]
    retained = [d for d in predictions if d not in ignored]
    truth = labels["points"]
    matches = match_points(truth, retained, width, tolerance)
    color_matches = match_points(truth, retained, width, tolerance, color_aware=True)
    known_color = [
        (truth[m["truth_index"]], retained[m["prediction_index"]])
        for m in matches
        if truth[m["truth_index"]]["color"] != "unknown"
    ]
    return {
        "true_positive": len(matches),
        "false_positive": len(retained) - len(matches),
        "false_negative": len(truth) - len(matches),
        "truth_count": len(truth),
        "prediction_count": len(retained),
        "ignored_predictions": len(ignored),
        "color_aware_true_positive": len(color_matches),
        "color_aware_false_positive": len(retained) - len(color_matches),
        "color_aware_false_negative": len(truth) - len(color_matches),
        "known_color_matches": len(known_color),
        "correct_colors": sum(a["color"] == b["color"] for a, b in known_color),
        "matches": matches,
        "color_aware_matches": color_matches,
        "empty_view_correct": not truth and not retained,
    }


def summarize(rows):
    keys = [
        "true_positive",
        "false_positive",
        "false_negative",
        "truth_count",
        "prediction_count",
        "ignored_predictions",
        "color_aware_true_positive",
        "color_aware_false_positive",
        "color_aware_false_negative",
        "known_color_matches",
        "correct_colors",
    ]
    totals = {key: sum(r[key] for r in rows) for key in keys}
    tp, fp, fn = [totals[k] for k in ("true_positive", "false_positive", "false_negative")]
    ctp, cfp, cfn = [
        totals[k]
        for k in ("color_aware_true_positive", "color_aware_false_positive", "color_aware_false_negative")
    ]
    errors = [m["error_fraction"] for r in rows for m in r["matches"]]
    return {
        **totals,
        "views": len(rows),
        "precision": tp / (tp + fp) if tp + fp else None,
        "recall": tp / (tp + fn) if tp + fn else None,
        "f1": 2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else None,
        "color_aware_f1": 2 * ctp / (2 * ctp + cfp + cfn) if 2 * ctp + cfp + cfn else None,
        "color_accuracy_on_matched_known_colors": totals["correct_colors"] / totals["known_color_matches"]
        if totals["known_color_matches"]
        else None,
        "median_center_error_fraction": float(np.median(errors)) if errors else None,
        "p95_center_error_fraction": float(np.quantile(errors, 0.95)) if errors else None,
        "empty_views_correctly_empty": sum(r["empty_view_correct"] for r in rows),
    }


def evaluate_dataset(db, request: EvaluationRequest):
    version = request.model_version or VERSION
    with db.session() as session:
        dataset = session.get(EvaluationDataset, request.dataset_id)
        if dataset is None:
            raise ValueError("Frozen evaluation dataset not found")
        frozen, dataset_sha = dataset.document, dataset.sha256
    records = [r for r in frozen["records"] if r["partition"] == request.partition]
    if not records:
        raise ValueError("The requested partition has no eligible frozen label views")
    rows, exclusions = [], []
    for record in records:
        labels, frame = record["labels"], record["evidence"]
        identifier = str(uuid5(NAMESPACE_URL, f"{labels['evidence_id']}:{version}"))
        with db.session() as session:
            observation = session.get(FrameObservation, identifier)
            if observation is None and version == VERSION:
                evidence = session.get(Evidence, labels["evidence_id"])
                if evidence is not None:
                    try:
                        analyze_frame(session, evidence, db.settings.data_dir)
                        session.flush()
                        observation = session.get(FrameObservation, identifier)
                    except (ValueError, OSError) as exc:
                        exclusions.append({"labels_id": record["labels_id"], "reason": str(exc)})
                        continue
            if observation is None:
                exclusions.append(
                    {"labels_id": record["labels_id"], "reason": "model_predictions_not_processed"}
                )
                continue
            predicted = observation.document["views"].get(labels["view"])
            asset = frame["crops"][labels["view"]]
            if (
                predicted is None
                or predicted["width"] != asset["width"]
                or predicted["height"] != asset["height"]
            ):
                exclusions.append({"labels_id": record["labels_id"], "reason": "prediction_crop_mismatch"})
                continue
            # Even a frozen dataset cannot make a deleted/corrupt source image reproducible.
            path = db.settings.data_dir / asset["path"]
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != asset["sha256"]:
                exclusions.append({"labels_id": record["labels_id"], "reason": "evidence_missing_or_corrupt"})
                continue
            row = evaluate_view(
                labels, predicted["detections"], asset["width"], request.max_center_error_fraction
            )
            rows.append(
                {
                    **row,
                    "labels_id": record["labels_id"],
                    "label_revision": record["label_revision"],
                    "evidence_id": labels["evidence_id"],
                    "view": labels["view"],
                    "evaluation_group": labels["evaluation_group"],
                    "sheet": record["source_manifest"].get("sheet"),
                    "provenance": labels["provenance"],
                    "resolution": f"{asset['width']}x{asset['height']}",
                    "prediction_observation_id": observation.id,
                    "prediction_sha256": digest(observation.document),
                    "youtube_url": frame.get("youtube_url"),
                }
            )
    if not rows:
        raise ValueError(
            "No reproducible processed predictions in this partition; analyze retained frames first"
        )
    breakdowns = {}
    for field in ("sheet", "resolution", "provenance", "evaluation_group"):
        breakdowns[field] = {
            str(value): summarize([r for r in rows if r[field] == value])
            for value in sorted({r[field] for r in rows}, key=str)
        }
    document = {
        "schema_version": "handle-detection-evaluation-v1",
        "created_at": now(),
        "dataset_id": request.dataset_id,
        "dataset_sha256": dataset_sha,
        "dataset_name": frozen["name"],
        "mode": frozen["mode"],
        "model_version": version,
        "partition": request.partition,
        "definition": {
            "object_kind": "colored_handle_center",
            "matching": "one_to_one_max_cardinality_min_distance",
            "max_center_error_fraction": request.max_center_error_fraction,
            "error_unit": "fraction_of_crop_width",
            "eligibility": "complete_visible reviewed frozen label views; ignored regions excluded",
            "scope": "image-space handle detection; not granite localization, score accuracy, or identity",
            "uncertainty_note": (
                "Adjacent frames and shared days are correlated; this report "
                "does not establish release-gate confidence."
            ),
            "auto_acceptance_authority": False,
        },
        "coverage": {
            "eligible_frozen_views": len(records),
            "evaluated_views": len(rows),
            "excluded_views": len(exclusions),
        },
        "summary": summarize(rows),
        "breakdowns": breakdowns,
        "rows": rows,
        "exclusions": exclusions,
    }
    with db.session() as session:
        run = EvaluationRun(
            dataset_id=request.dataset_id, model_version=version, sha256=digest(document), document=document
        )
        session.add(run)
        session.flush()
        return {"id": run.id, "sha256": run.sha256, **document}


def run_catalog(db):
    with db.session() as session:
        return [
            {
                "id": r.id,
                "dataset_id": r.dataset_id,
                "model_version": r.model_version,
                "sha256": r.sha256,
                "created_at": r.created_at,
                "partition": r.document["partition"],
                "mode": r.document["mode"],
                "summary": r.document["summary"],
                "coverage": r.document["coverage"],
            }
            for r in session.scalars(select(EvaluationRun))
        ]
