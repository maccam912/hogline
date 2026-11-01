"""Preview and clear machine analysis without deleting source media or human reviews."""

from pydantic import Field
from sqlalchemy import delete, select

from hogline import db as tables
from hogline.adapters.sources import digest
from hogline.domain.catalog import Conflict, source_or_error
from hogline.domain.models import Model


class ResetRequest(Model):
    expected_sha256: str = Field(min_length=64, max_length=64)


def _contains(value, identifiers):
    if isinstance(value, str):
        return value in identifiers
    if isinstance(value, dict):
        return any(_contains(v, identifiers) for v in value.values())
    if isinstance(value, list):
        return any(_contains(v, identifiers) for v in value)
    return False


def reset_plan(session, source_id):
    source_or_error(session, source_id)
    models = [
        tables.Job,
        tables.ProcessingBatch,
        tables.JobExecution,
        tables.Scan,
        tables.AnalysisWindow,
        tables.ScoringSegment,
        tables.FrameObservation,
        tables.WindowTracks,
        tables.WindowHouseStates,
        tables.WindowActors,
        tables.WindowDeliveries,
    ]
    selected = {m.__table__.name: {} for m in models}
    blockers = []
    evidence = set(session.scalars(select(tables.Evidence.id).where(tables.Evidence.source_id == source_id)))
    for model in models:
        if model in (tables.Job, tables.ProcessingBatch):
            identifiers = [
                identifier
                for identifier, request in session.execute(select(model.id, model.document["request"]))
                if _contains(request, {source_id})
            ]
            rows = tables.rows_by_ids(session, model, model.id, identifiers)
        elif hasattr(model, "source_id"):
            rows = session.scalars(select(model).where(model.source_id == source_id))
        elif model is tables.FrameObservation:
            rows = tables.rows_by_ids(session, model, model.evidence_id, evidence)
        else:
            continue
        for row in rows:
            doc = row.document
            belongs = getattr(row, "source_id", None) == source_id
            if model in (tables.Job, tables.ProcessingBatch):
                belongs = _contains(doc.get("request", {}), {source_id})
                if belongs and row.state in ("active", "paused", "queued", "running"):
                    blockers.append(
                        "Cancel this recording’s queued or paused jobs and wait for running tasks "
                        "to stop in Jobs & progress."
                    )
                if doc.get("kind", "").startswith(("lineup", "skill")):
                    belongs = False
                if belongs and len(doc.get("request", {}).get("source_ids", [source_id])) > 1:
                    blockers.append(
                        "This analysis shares a batch with other recordings. "
                        "Shared batches cannot be cleared here."
                    )
            if model is tables.FrameObservation:
                belongs = row.evidence_id in evidence and doc.get("kind") != "independent-visual-ice-end-v1"
            if belongs:
                selected[model.__table__.name][row.id] = row
    # Include dependent machine results in foreign-key order. Unknown dependents
    # block the operation instead of silently deleting reviews or named people.
    changed = True
    while changed:
        changed = False
        for model in models:
            for fk in model.__table__.foreign_keys:
                parent_ids = selected.get(fk.column.table.name, {})
                if not parent_ids:
                    continue
                for row in tables.rows_by_ids(session, model, getattr(model, fk.parent.name), parent_ids):
                    if row.id not in selected[model.__table__.name]:
                        selected[model.__table__.name][row.id] = row
                        changed = True
    ids = {identifier for rows in selected.values() for identifier in rows}
    for mapper in tables.Base.registry.mappers:
        model = mapper.class_
        if model in models or model in (tables.Source, tables.MetadataSnapshot, tables.Evidence):
            continue
        if model.__table__.name == "recording_campaign":
            continue  # Historical progress can show that its analysis was cleared.
        for row in session.scalars(select(model)):
            linked = any(
                getattr(row, fk.parent.name) in selected.get(fk.column.table.name, {})
                for fk in model.__table__.foreign_keys
            )
            if linked or _contains(getattr(row, "document", {}), ids):
                blockers.append(
                    f"Saved {model.__table__.name.replace('_', ' ')} data depends on this analysis. "
                    "Keep the analysis and use targeted review or face rescan instead."
                )
                break
    records = {
        name: [
            {c.name: getattr(row, c.name) for c in row.__table__.columns} for _, row in sorted(rows.items())
        ]
        for name, rows in selected.items()
    }
    return selected, {
        "source_id": source_id,
        "counts": {name: len(rows) for name, rows in selected.items() if rows},
        "blockers": sorted(set(blockers)),
        "sha256": digest(records),
        "kept": (
            "Original video, cached frames, saved games and scores, independent end reviews, "
            "names and face scans are kept. This does not free disk space."
        ),
    }


def register_reset(app, db):
    @app.get("/api/recordings/{source_id}/reset-preview")
    def preview(source_id: str):
        with db.snapshot_session() as session:
            return reset_plan(session, source_id)[1]

    @app.post("/api/recordings/{source_id}/reset")
    def reset(source_id: str, body: ResetRequest):
        with db.snapshot_session(write=True) as session:
            rows, plan = reset_plan(session, source_id)
            if plan["sha256"] != body.expected_sha256:
                raise Conflict("Analysis changed. Reopen Clear analysis to preview the current data.")
            if plan["blockers"]:
                raise ValueError(" ".join(plan["blockers"]))
            for table in reversed(tables.Base.metadata.sorted_tables):
                identifiers = list(rows.get(table.name, {}))
                for start in range(0, len(identifiers), 500):
                    session.execute(delete(table).where(table.c.id.in_(identifiers[start : start + 500])))
            return {"cleared": plan["counts"], "source_id": source_id}
