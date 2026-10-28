from sqlalchemy import select

from hogline.adapters.sources import digest
from hogline.db import EvaluationDataset, Evidence, FrameLabels, Source, now
from hogline.evaluation.models import DatasetCreate


def freeze_dataset(db, request: DatasetCreate):
    if not request.partitions:
        raise ValueError("Assign at least one whole recording/day group to a partition")
    records, exclusions = [], []
    with db.snapshot_session(write=True) as session:
        sources = {s.id: s.document for s in session.scalars(select(Source))}
        evidence = {e.id: e.document for e in session.scalars(select(Evidence))}
        source_partitions, day_partitions = {}, {}
        for row in session.scalars(select(FrameLabels)):
            label = row.document
            group = label["evaluation_group"]
            if group not in request.partitions:
                continue
            frame = evidence[row.evidence_id]
            source = sources[frame["source_id"]]
            partition = request.partitions[group]
            # Adjacent views/frames of a recording and all known same-day uploads
            # stay together. Explicit groups must also join known duplicate families.
            for registry, key in (
                (source_partitions, frame["source_id"]),
                (day_partitions, source.get("title_date") or frame["source_id"]),
            ):
                previous = registry.setdefault(key, partition)
                if previous != partition:
                    raise ValueError(
                        "Split leakage: a recording or known recording day occurs in multiple partitions"
                    )
            reason = None
            if label["coverage"] != "complete_visible" or label["review_status"] != "reviewed":
                reason = "view_not_complete_and_reviewed"
            elif label["provenance"] == "model_seeded":
                reason = "unverified_model_seed"
            elif request.mode == "confirmed" and label["provenance"] != "human":
                reason = "nonhuman_labels_excluded_from_confirmed_mode"
            if reason:
                exclusions.append({"labels_id": row.id, "revision": row.revision, "reason": reason})
                continue
            records.append(
                {
                    "labels_id": row.id,
                    "label_revision": row.revision,
                    "labels": label,
                    "partition": partition,
                    "evidence": frame,
                    "source_manifest": source,
                }
            )
        if not records:
            raise ValueError("No eligible complete reviewed label views in the requested groups/mode")
        present = {r["labels"]["evaluation_group"] for r in records}
        missing = set(request.partitions) - present
        if missing:
            raise ValueError(
                "Some assigned groups have no eligible reviewed views: " + ", ".join(sorted(missing))
            )
        document = {
            "schema_version": "handle-evaluation-dataset-v1",
            "name": request.name,
            "created_at": now(),
            "author": request.author,
            "reason": request.reason,
            "mode": request.mode,
            "partitions": request.partitions,
            "records": records,
            "exclusions": exclusions,
            "grouping_limitations": (
                "Title-day isolation is conservative; verify midnight games "
                "and duplicate families before freezing."
            ),
        }
        row = EvaluationDataset(sha256=digest(document), document=document)
        session.add(row)
        session.flush()
        return {"id": row.id, "sha256": row.sha256, **document}


def dataset_catalog(db):
    with db.session() as session:
        return [
            {
                "id": d.id,
                "sha256": d.sha256,
                "name": d.document["name"],
                "mode": d.document["mode"],
                "partitions": d.document["partitions"],
                "record_count": len(d.document["records"]),
                "created_at": d.created_at,
            }
            for d in session.scalars(select(EvaluationDataset))
        ]
