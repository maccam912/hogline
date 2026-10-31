"""Preserve skill evidence only across audited, monotonic coverage extensions."""

from pydantic import Field, model_validator
from sqlalchemy import select

from hogline.db import GameRow, Revision
from hogline.domain.catalog import Conflict, save_in_session
from hogline.domain.models import Game, GameEdit, Model


def widening_only(before, after):
    if {k: v for k, v in before.items() if k != "spans"} != {k: v for k, v in after.items() if k != "spans"}:
        return False
    old, new = before.get("spans", []), after.get("spans", [])
    if not old or len(old) != len(new):
        return False
    for a, b in zip(old, new, strict=True):
        fields = {"start_seconds", "end_seconds"}
        if {k: v for k, v in a.items() if k not in fields} != {k: v for k, v in b.items() if k not in fields}:
            return False
        if b["start_seconds"] > a["start_seconds"] or b["end_seconds"] < a["end_seconds"]:
            return False
    return True


def compatible_game_scope(session, game, expected_revision):
    if game is None or game.document.get("retired_into"):
        return False
    if game.revision == expected_revision:
        return True
    if expected_revision > game.revision:
        return False
    revisions = list(
        session.scalars(
            select(Revision)
            .where(
                Revision.game_id == game.id,
                Revision.number >= expected_revision,
                Revision.number <= game.revision,
            )
            .order_by(Revision.number)
        )
    )
    if [r.number for r in revisions] != list(range(expected_revision, game.revision + 1)):
        return False
    if revisions[-1].document != game.document:
        return False
    return all(widening_only(a.document, b.document) for a, b in zip(revisions, revisions[1:], strict=False))


class CoverageExtension(Model):
    expected_game_revision: int = Field(ge=1)
    span_index: int = Field(ge=0)
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(gt=0)

    @model_validator(mode="after")
    def ordered(self):
        if self.end_seconds <= self.start_seconds:
            raise ValueError("Coverage end must be after start")
        return self


def extend_skill_coverage(db, game_id, request):
    with db.snapshot_session(write=True) as session:
        row = session.get(GameRow, game_id)
        if row is None or row.document.get("retired_into"):
            raise ValueError("Choose an active game")
        if row.revision != request.expected_game_revision:
            raise Conflict("Game coverage changed; reload before extending it")
        game = Game.model_validate(row.document)
        if game.acceptance == "human_confirmed" or game.coverage == "complete":
            raise ValueError("Use game boundary review to change an accepted or complete game")
        if request.span_index >= len(game.spans):
            raise ValueError("Source span is absent")
        span = game.spans[request.span_index]
        if request.start_seconds > span.start_seconds or request.end_seconds < span.end_seconds:
            raise ValueError("Extend coverage without removing existing source time")
        if request.start_seconds == span.start_seconds and request.end_seconds == span.end_seconds:
            return {"game_revision": row.revision, "spans": [s.model_dump() for s in game.spans]}
        span.start_seconds, span.end_seconds = request.start_seconds, request.end_seconds
        result = save_in_session(
            session,
            GameEdit(
                expected_revision=row.revision,
                game=game,
                author="Skill coverage review",
                reason=(
                    "Extend analysis coverage only; existing game interpretation and evidence "
                    "remain unchanged"
                ),
            ),
        )
        return {"game_revision": result["revision"], "spans": result["spans"]}
