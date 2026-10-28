from fastapi import HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import select

from hogline.db import BoardDataset, BoardEvaluation, BoardLabelRevision, BoardLabels
from hogline.evaluation.boards import (
    BoardEvaluationRequest,
    BoardLabelEdit,
    board_evaluation_catalog,
    evaluate_board_dataset,
    freeze_board_dataset,
    save_board_labels,
)
from hogline.evaluation.models import DatasetCreate


def register_board_evaluation(app, db):
    @app.get("/api/board-labels/{evidence_id}")
    def get_board_labels(evidence_id: str):
        with db.session() as session:
            row = session.get(BoardLabels, evidence_id)
            return {
                "labels": {"id": row.id, "revision": row.revision, **row.document} if row else None,
                "history": [
                    {
                        "revision": r.number,
                        "author": r.author,
                        "reason": r.reason,
                        "created_at": r.created_at,
                        "labels": r.document,
                    }
                    for r in session.scalars(
                        select(BoardLabelRevision)
                        .where(BoardLabelRevision.labels_id == evidence_id)
                        .order_by(BoardLabelRevision.number.desc())
                    )
                ],
            }

    @app.put("/api/board-labels/{evidence_id}")
    def put_board_labels(evidence_id: str, body: BoardLabelEdit):
        return save_board_labels(db, evidence_id, body)

    @app.get("/api/board-evaluation")
    def get_board_evaluation():
        return board_evaluation_catalog(db)

    @app.post("/api/board-evaluation/datasets")
    def create_board_dataset(body: DatasetCreate):
        return freeze_board_dataset(db, body)

    @app.post("/api/board-evaluation/runs")
    def create_board_evaluation(body: BoardEvaluationRequest):
        return evaluate_board_dataset(db, body)

    @app.get("/api/board-evaluation/{kind}/{identifier}.json")
    def export_board_evaluation(kind: str, identifier: str):
        table = {"datasets": BoardDataset, "runs": BoardEvaluation}.get(kind)
        if table is None:
            raise HTTPException(404, "Choose datasets or runs")
        with db.session() as session:
            row = session.get(table, identifier)
            if row is None:
                raise HTTPException(404, "Board evaluation artifact not found")
            return JSONResponse(
                {"id": row.id, "sha256": row.sha256, **row.document},
                headers={
                    "Content-Disposition": f'attachment; filename="hogline-board-{kind}-{identifier}.json"',
                },
            )
