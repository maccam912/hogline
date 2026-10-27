from sqlalchemy import select
from sqlalchemy.orm.exc import StaleDataError

from hogline.adapters.sources import video_link
from hogline.db import Evidence, GameRow, ReviewTask, Revision, Scan, Source
from hogline.domain.models import Game, GameEdit


class Conflict(ValueError):
    pass


def source_or_error(session, source_id):
    source = session.get(Source, source_id)
    if source is None:
        raise ValueError("Source not found")
    return source.document


def validate_evidence(session, game):
    for span in game.spans:
        source = source_or_error(session, span.source_id)
        if source.get("sheet") is not None and source["sheet"] != game.sheet:
            raise ValueError("A source span belongs to a different sheet")
        duration = source.get("duration_seconds")
        if duration is not None and span.end_seconds > duration + 0.1:
            raise ValueError("Source span extends past the recording")
    ids = {e for end in game.ends for e in end.evidence_ids} | set(game.result.evidence_ids)
    for evidence_id in ids:
        evidence = session.get(Evidence, evidence_id)
        if evidence is None:
            raise ValueError(f"Evidence frame not found: {evidence_id}")
        frame = evidence.document
        if not any(
            s.source_id == evidence.source_id
            and s.start_seconds - 0.1 <= frame["source_seconds"] <= s.end_seconds + 0.1
            for s in game.spans
        ):
            raise ValueError("Score evidence must fall inside a linked source span")


def save_in_session(session, edit: GameEdit, origin_key=None):
    game = edit.game
    validate_evidence(session, game)
    row = session.get(GameRow, game.id)
    if row is None:
        if edit.expected_revision != 0:
            raise Conflict("Game no longer exists; reload the catalog")
        row = GameRow(id=game.id, origin_key=origin_key, document=game.model_dump())
        session.add(row)
        session.flush()
    else:
        if row.revision != edit.expected_revision:
            raise Conflict("This game changed in another review. Reload before saving.")
        # Force a version increment even when saving an unchanged human decision.
        row.document = game.model_dump()
        from sqlalchemy.orm.attributes import flag_modified

        flag_modified(row, "document")
        try:
            session.flush()
        except StaleDataError as exc:
            raise Conflict("Concurrent edit detected; reload before saving") from exc
    session.add(
        Revision(
            game_id=row.id,
            number=row.revision,
            document=game.model_dump(),
            author=edit.author,
            reason=edit.reason,
        )
    )
    if game.acceptance == "proposed":
        key = f"game:{row.id}:boundary"
        task = session.scalar(select(ReviewTask).where(ReviewTask.task_key == key))
        if task is None:
            session.add(ReviewTask(task_key=key, game_id=row.id, reason="possible_new_game"))
        else:
            task.state = "open"
    else:
        for task in session.scalars(select(ReviewTask).where(ReviewTask.game_id == row.id)):
            task.state = "resolved" if game.acceptance == "human_confirmed" else "open"
    return {**game.model_dump(), "revision": row.revision}


def save_game(db, edit):
    with db.session() as session:
        return save_in_session(session, edit)


def present_game(row, sources):
    game = {**row.document, "revision": row.revision}
    game["spans"] = [
        {**s, "youtube_url": video_link(sources[s["source_id"]], s["start_seconds"])} for s in game["spans"]
    ]
    return game


def propose_games(session, source, scan, job_id):
    if not source.get("sheet"):
        return []  # A sheet/date title is only a hint; unknown-sheet sources stay in inventory.
    game_ids = []
    existing = list(session.scalars(select(GameRow)))
    for index, candidate in enumerate(scan["candidates"]):
        start, end = candidate["start_seconds"], candidate["end_seconds"]
        origin = f"{job_id}:{index}"
        match = None
        for row in existing:
            if row.origin_key == origin:
                match = row
                break
            for span in row.document["spans"]:
                overlap = max(0, min(end, span["end_seconds"]) - max(start, span["start_seconds"]))
                smaller = min(end - start, span["end_seconds"] - span["start_seconds"])
                if span["source_id"] == source["id"] and overlap / max(smaller, 0.001) >= 0.6:
                    match = row
                    break
            if match:
                break
        if match:
            # Never overwrite human spans, scores, or a retired interpretation on a rerun.
            game_ids.append(match.id)
            continue
        game = Game(
            sheet=source["sheet"],
            label=f"Activity candidate · sheet {source['sheet']}",
            coverage="partial",
            spans=[
                {
                    "source_id": source["id"],
                    "start_seconds": start,
                    "end_seconds": end,
                    "boundary_uncertainty_seconds": candidate["boundary_uncertainty_seconds"],
                }
            ],
            note="Sparse motion only. Could be play, practice, maintenance, or neighboring-sheet activity.",
        )
        result = save_in_session(
            session,
            GameEdit(
                expected_revision=0,
                author="activity-baseline",
                reason="Unvalidated motion proposal; requires review",
                game=game,
            ),
            origin,
        )
        game_ids.append(result["id"])
        existing.append(session.get(GameRow, game.id))
    return game_ids


def catalog(db):
    with db.session() as session:
        sources = {r.id: r.document for r in session.scalars(select(Source))}
        games = [
            present_game(r, sources)
            for r in session.scalars(select(GameRow))
            if not r.document.get("retired_into")
        ]
        scans = [{"id": r.id, "source_id": r.source_id, **r.document} for r in session.scalars(select(Scan))]
        evidence = [{"id": r.id, **r.document} for r in session.scalars(select(Evidence))]
        tasks = [
            {"id": r.id, "game_id": r.game_id, "reason": r.reason, "state": r.state}
            for r in session.scalars(select(ReviewTask))
        ]
        return {
            "sources": list(sources.values()),
            "games": games,
            "scans": scans,
            "evidence": evidence,
            "review_tasks": tasks,
        }
