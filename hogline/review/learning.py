from fastapi import HTTPException
from sqlalchemy import select

from hogline.adapters.sources import digest
from hogline.db import HandleModel, HandleTrainingRun
from hogline.learning.handles import HandleTrainingRequest, checked_model, train_handle_model


def model_catalog(db):
    with db.snapshot_session() as session:
        return [
            checked_model(r)
            for r in session.scalars(select(HandleModel).order_by(HandleModel.created_at.desc()))
        ]


def register_learning(app, db):
    @app.get("/api/handle-models")
    def models():
        return {"models": model_catalog(db), "default_detector_changed": False}

    @app.post("/api/handle-models/train")
    def train(body: HandleTrainingRequest):
        return train_handle_model(db, body)

    @app.get("/api/handle-models/runs/{identifier}.json")
    def training_run(identifier: str):
        with db.snapshot_session() as session:
            row = session.get(HandleTrainingRun, identifier)
            if row is None:
                raise HTTPException(404, "Handle training run not found")
            if digest(row.document) != row.sha256:
                raise ValueError("Handle training run checksum mismatch")
            return {"id": row.id, "sha256": row.sha256, **row.document}
