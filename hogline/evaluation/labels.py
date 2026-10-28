from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.orm.exc import StaleDataError

from hogline.db import Evidence, FrameLabels, LabelRevision, Source
from hogline.domain.catalog import Conflict
from hogline.evaluation.models import LabelEdit


def label_id(evidence_id, view):
    return str(uuid5(NAMESPACE_URL, f"labels:{evidence_id}:{view}"))


def inside(point, region):
    x, y = point
    return (
        region["x"] <= x <= region["x"] + region["width"]
        and region["y"] <= y <= region["y"] + region["height"]
    )


def save_labels(db, edit: LabelEdit):
    labels = edit.labels
    with db.session() as session:
        evidence = session.get(Evidence, labels.evidence_id)
        if evidence is None:
            raise ValueError("Evidence frame not found")
        asset = evidence.document["crops"].get(labels.view)
        if asset is None:
            raise ValueError("Requested overhead view is not present in this evidence revision")
        width, height = asset["width"], asset["height"]
        for point in labels.points:
            x, y = point.center_px
            if not (0 <= x < width and 0 <= y < height):
                raise ValueError("A label center falls outside the original crop pixels")
            if any(inside(point.center_px, r.model_dump()) for r in labels.ignore_regions):
                raise ValueError("A measured point cannot also be inside an ignored region")
            if any(inside(point.center_px, r.model_dump()) for r in labels.negative_regions):
                raise ValueError("A measured point cannot also be inside a known non-stone region")
        for region in labels.ignore_regions + labels.negative_regions:
            if region.x + region.width > width or region.y + region.height > height:
                raise ValueError("Annotation regions must stay inside the original crop")
        identifier = label_id(labels.evidence_id, labels.view)
        row = session.get(FrameLabels, identifier)
        if row is None:
            if edit.expected_revision != 0:
                raise Conflict("Labels no longer exist; reload before editing")
            row = FrameLabels(
                id=identifier, evidence_id=labels.evidence_id, view=labels.view, document=labels.model_dump()
            )
            session.add(row)
        else:
            if row.revision != edit.expected_revision:
                raise Conflict("These labels changed in another review; reload before saving")
            row.document = labels.model_dump()
            flag_modified(row, "document")
        try:
            session.flush()
        except StaleDataError as exc:
            raise Conflict("Concurrent label edit; reload before saving") from exc
        session.add(
            LabelRevision(
                labels_id=identifier,
                number=row.revision,
                document=row.document,
                author=edit.author,
                reason=edit.reason,
            )
        )
        return {"id": row.id, "revision": row.revision, **row.document}


def labels_catalog(db):
    with db.session() as session:
        rows = [
            {"id": r.id, "revision": r.revision, **r.document} for r in session.scalars(select(FrameLabels))
        ]
        sources = {r.id: r.document for r in session.scalars(select(Source))}
        evidence = {r.id: r for r in session.scalars(select(Evidence))}
        groups = {}
        for row in rows:
            group = groups.setdefault(
                row["evaluation_group"],
                {"label_views": 0, "complete_reviewed_views": 0, "source_ids": set(), "provenances": set()},
            )
            group["label_views"] += 1
            group["complete_reviewed_views"] += (
                row["coverage"] == "complete_visible" and row["review_status"] == "reviewed"
            )
            group["source_ids"].add(evidence[row["evidence_id"]].source_id)
            group["provenances"].add(row["provenance"])
        return {
            "labels": rows,
            "groups": [
                {
                    "id": key,
                    **value,
                    "source_ids": sorted(value["source_ids"]),
                    "provenances": sorted(value["provenances"]),
                }
                for key, value in sorted(groups.items())
            ],
            "suggested_source_groups": {
                sid: f"wausau:{s.get('title_date') or sid}" for sid, s in sources.items()
            },
        }
