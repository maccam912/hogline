from fastapi import HTTPException
from sqlalchemy import select

from hogline.adapters.sources import digest
from hogline.analytics.tracks import load_track_input
from hogline.db import AnalysisWindow, HouseDataset, HouseEvaluation, HouseLabelRevision, HouseLabels
from hogline.evaluation.house_states import (
    HouseEvaluationRequest,
    HouseLabelEdit,
    View,
    evaluate_house_dataset,
    freeze_house_dataset,
    label_id,
    save_house_labels,
)
from hogline.evaluation.models import DatasetCreate
from hogline.reconstruction.house_states import VERSION, anchor


def register_house_evaluation(app, db):
    @app.get("/api/house-labels/{window_id}/{view}")
    def get_house_labels(window_id: str, view: View):
        with db.snapshot_session() as session:
            window_row = session.get(AnalysisWindow, window_id)
            if window_row is None:
                raise HTTPException(404, "Detailed window not found")
            window = {**window_row.document, "id": window_id, "source_id": window_row.source_id}
            samples = [anchor(r) for r in load_track_input(db.settings.data_dir, window) if r["view"] == view]
            identifier = label_id(window_id, view)
            row = session.get(HouseLabels, identifier)
            return {
                "labels": {"id": row.id, "revision": row.revision, **row.document} if row else None,
                "window_id": window_id,
                "view": view,
                "source_id": window["source_id"],
                "samples": samples,
                "context_anchor_evidence_ids": window["anchor_evidence_ids"],
                "history": [
                    {
                        "revision": r.number,
                        "author": r.author,
                        "reason": r.reason,
                        "created_at": r.created_at,
                        "labels": r.document,
                    }
                    for r in session.scalars(
                        select(HouseLabelRevision)
                        .where(
                            HouseLabelRevision.labels_id == identifier,
                        )
                        .order_by(HouseLabelRevision.number.desc())
                    )
                ],
            }

    @app.put("/api/house-labels/{window_id}/{view}")
    def put_house_labels(window_id: str, view: View, body: HouseLabelEdit):
        return save_house_labels(db, window_id, view, body)

    @app.get("/api/house-evaluation")
    def get_house_evaluation():
        with db.snapshot_session() as session:
            return {
                "version": VERSION,
                "labels": [
                    {"id": r.id, "revision": r.revision, **r.document}
                    for r in session.scalars(select(HouseLabels).order_by(HouseLabels.id))
                ],
                "datasets": [
                    {
                        "id": r.id,
                        "sha256": r.sha256,
                        "name": r.document["name"],
                        "mode": r.document["mode"],
                        "partitions": r.document["partitions"],
                        "record_count": len(r.document["records"]),
                    }
                    for r in session.scalars(select(HouseDataset).order_by(HouseDataset.created_at.desc()))
                ],
                "runs": [
                    {
                        "id": r.id,
                        "sha256": r.sha256,
                        "dataset_id": r.dataset_id,
                        "model_version": r.document["model_version"],
                        "partition": r.document["partition"],
                        "summary": r.document["summary"],
                        "created_at": r.created_at,
                    }
                    for r in session.scalars(
                        select(HouseEvaluation).order_by(HouseEvaluation.created_at.desc())
                    )
                ],
            }

    @app.post("/api/house-evaluation/datasets")
    def create_house_dataset(body: DatasetCreate):
        return freeze_house_dataset(db, body)

    @app.post("/api/house-evaluation/runs")
    def create_house_evaluation(body: HouseEvaluationRequest):
        return evaluate_house_dataset(db, body)

    @app.get("/api/house-evaluation/{kind}/{identifier}.json")
    def export_house_evaluation(kind: str, identifier: str):
        table = {"datasets": HouseDataset, "runs": HouseEvaluation}.get(kind)
        if table is None:
            raise HTTPException(404, "Choose datasets or runs")
        with db.session() as session:
            row = session.get(table, identifier)
            if row is None:
                raise HTTPException(404, "House evaluation artifact not found")
            if digest(row.document) != row.sha256:
                raise ValueError("House evaluation artifact checksum mismatch")
            return {"id": row.id, "sha256": row.sha256, **row.document}
