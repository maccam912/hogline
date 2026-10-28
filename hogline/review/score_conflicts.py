"""Append-only reviewer decisions tied to the exact discrepancy and game revision."""

from typing import Literal

from pydantic import Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from hogline.adapters.sources import digest
from hogline.db import ScoreConflictReview, now
from hogline.domain.catalog import Conflict, catalog_in_session
from hogline.domain.models import Model
from hogline.perception.broadcast import broadcast_catalog_in_session
from hogline.reconstruction.score_conflicts import VERSION, score_conflicts


class ScoreConflictDecision(Model):
    expected_review_revision: int = Field(ge=0)
    decision: Literal["keep_saved_score", "needs_review"]
    author: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=1, max_length=2000)

    @field_validator("author", "reason")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Reviewer and reason cannot be blank")
        return value.strip()


def review_history(session):
    reviews = []
    for row in session.scalars(
        select(ScoreConflictReview).order_by(ScoreConflictReview.created_at, ScoreConflictReview.id)
    ):
        if digest(row.document) != row.sha256:
            raise ValueError("Stored score-disagreement review checksum mismatch")
        reviews.append({"id": row.id, "sha256": row.sha256, **row.document})
    return reviews


def conflicts_in_session(session, data, broadcast):
    history = review_history(session)
    current = []
    for conflict in score_conflicts(data, broadcast):
        reviews = [r for r in history if r["conflict"]["id"] == conflict["id"]]
        latest = max(reviews, key=lambda r: r["review_revision"], default=None)
        current.append(
            {
                **conflict,
                "review_revision": latest["review_revision"] if latest else 0,
                "review_state": "acknowledged"
                if latest and latest["decision"] == "keep_saved_score"
                else "open",
                "latest_review": latest,
            }
        )
    return {
        "version": VERSION,
        "conflicts": current,
        "reviews": history,
        "open_count": sum(c["review_state"] == "open" for c in current),
        "notice": (
            "Acknowledgment keeps the saved score and its existing acceptance. "
            "New game revisions or different proposal evidence require a new review."
        ),
    }


def get_score_conflicts(db):
    with db.snapshot_session() as session:
        return conflicts_in_session(
            session, catalog_in_session(session), broadcast_catalog_in_session(session, db.settings.data_dir)
        )


def save_score_conflict_review(db, conflict_id, request):
    with db.snapshot_session(write=True) as session:
        result = conflicts_in_session(
            session, catalog_in_session(session), broadcast_catalog_in_session(session, db.settings.data_dir)
        )
        conflict = next((c for c in result["conflicts"] if c["id"] == conflict_id), None)
        if conflict is None:
            raise Conflict("The score discrepancy changed or no longer exists. Refresh before reviewing.")
        if conflict["review_revision"] != request.expected_review_revision:
            raise Conflict("This discrepancy was reviewed elsewhere. Refresh before saving.")
        frozen = {
            k: v for k, v in conflict.items() if k not in ("review_revision", "review_state", "latest_review")
        }
        document = {
            "conflict": frozen,
            "review_revision": conflict["review_revision"] + 1,
            "decision": request.decision,
            "author": request.author,
            "reason": request.reason,
            "created_at": now(),
        }
        row = ScoreConflictReview(
            conflict_id=conflict_id,
            revision=document["review_revision"],
            document=document,
            sha256=digest(document),
        )
        session.add(row)
        try:
            session.flush()
        except IntegrityError as exc:
            raise Conflict("This discrepancy was reviewed concurrently. Refresh before saving.") from exc
        return {"id": row.id, "sha256": row.sha256, **document}


def register_score_conflicts(app, db):
    @app.get("/api/score-disagreements")
    def get_disagreements():
        return get_score_conflicts(db)

    @app.post("/api/score-disagreements/{conflict_id}/reviews")
    def save_review(conflict_id: str, request: ScoreConflictDecision):
        return save_score_conflict_review(db, conflict_id, request)
