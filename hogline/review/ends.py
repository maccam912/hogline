"""End ledgers and exploratory house scoring in the local workbench."""

from fastapi import HTTPException
from pydantic import Field

from hogline.analytics.end_timeline import (
    analyze_house_scores,
    game_end_timeline,
    house_scores_in_session,
    source_end_timeline,
)
from hogline.domain.models import Model


class HouseScoreRequest(Model):
    limit: int = Field(default=24, ge=1, le=120)
    max_seconds: float = Field(default=30, gt=0, le=120)


def register_ends(app, db):
    @app.get("/api/sources/{source_id}/end-timeline")
    def get_source_ends(source_id: str, start: float | None = None, end: float | None = None):
        return source_end_timeline(db, source_id, start, end)

    @app.get("/api/games/{game_id}/end-timeline")
    def get_game_ends(game_id: str):
        return game_end_timeline(db, game_id)

    @app.post("/api/sources/{source_id}/house-scores")
    def create_scores(source_id: str, request: HouseScoreRequest):
        return analyze_house_scores(db, source_id, request.limit, request.max_seconds)

    @app.get("/api/sources/{source_id}/house-scores")
    def get_scores(source_id: str):
        with db.snapshot_session() as session:
            return {
                "observations": [s for s in house_scores_in_session(session) if s["source_id"] == source_id]
            }

    @app.get("/api/house-scores/{identifier}")
    def get_score(identifier: str):
        with db.snapshot_session() as session:
            result = next((r for r in house_scores_in_session(session) if r["id"] == identifier), None)
            if result is None:
                raise HTTPException(404, "House-score observation not found")
            return result
