"""Atomic, revision-checked game splits/merges with reversible lineage."""

from typing import Literal
from uuid import NAMESPACE_URL, uuid5

from pydantic import Field, model_validator
from sqlalchemy import select

from hogline.adapters.sources import digest
from hogline.db import GameRow, Reconciliation, Source, now
from hogline.domain.catalog import Conflict, save_in_session, validate_evidence
from hogline.domain.models import Game, GameEdit, Model, Result


class ParentRef(Model):
    game_id: str
    expected_revision: int = Field(ge=1)


class ReconcileRequest(Model):
    action: Literal["split", "merge"]
    parents: list[ParentRef] = Field(min_length=1, max_length=10)
    source_cuts: dict[str, float] = Field(default_factory=dict)
    end_choices: dict[str, str] = Field(default_factory=dict)
    label: str | None = Field(default=None, min_length=1, max_length=180)

    @model_validator(mode="after")
    def shape(self):
        if len({p.game_id for p in self.parents}) != len(self.parents):
            raise ValueError("Select each parent game once")
        if (self.action == "split" and len(self.parents) != 1) or (
            self.action == "merge" and len(self.parents) < 2
        ):
            raise ValueError("Split one game, or merge at least two games")
        if self.action == "merge" and self.source_cuts:
            raise ValueError("Merge does not use source cuts")
        return self


class ReconcileCommit(Model):
    request: ReconcileRequest
    preview_sha256: str = Field(min_length=64, max_length=64)
    author: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=1, max_length=2000)


class UndoReconciliation(Model):
    expected_revision: int = Field(ge=1)
    author: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=1, max_length=2000)


def operation_id(request):
    return str(uuid5(NAMESPACE_URL, "hogline:game-reconciliation-v1:" + digest(request.model_dump())))


def _parents(session, request):
    rows = []
    for parent in request.parents:
        row = session.get(GameRow, parent.game_id)
        if row is None or row.revision != parent.expected_revision or row.document.get("retired_into"):
            raise Conflict("A selected game changed or was retired; reload before reconciling")
        rows.append(row)
    if len({r.document["sheet"] for r in rows}) != 1:
        raise ValueError("Only games on the same sheet can be reconciled")
    return rows


def _draft(parent, identifier, label):
    game = Game.model_validate(parent).model_copy(deep=True)
    game.id, game.label = identifier, label[:200]
    game.acceptance, game.coverage, game.completion = "proposed", "partial", "unknown"
    game.result, game.ends, game.retired_into = Result(), [], []
    return game


def _copy_end(end, index):
    result = end.model_copy(deep=True)
    result.observed_index = index
    if result.acceptance == "human_confirmed":
        result.acceptance = "proposed"
    return result


def _split(session, request, parents, identifier):
    parent = Game.model_validate(parents[0].document)
    source_ids = {s.source_id for s in parent.spans}
    if set(request.source_cuts) != source_ids:
        raise ValueError(
            "Supply a split time for every linked recording; clocks are not assumed synchronized"
        )
    for source_id, cut in request.source_cuts.items():
        source = session.get(Source, source_id).document
        if cut < 0 or (source.get("duration_seconds") is not None and cut > source["duration_seconds"]):
            raise ValueError("Split times must fall within their source recordings")
    children = [
        _draft(
            parent.model_dump(),
            str(uuid5(NAMESPACE_URL, identifier + side)),
            (request.label or parent.label) + f" · {side} part",
        )
        for side in ("earlier", "later")
    ]
    for child in children:
        child.spans = []
    for span in parent.spans:
        cut = request.source_cuts[span.source_id]
        if span.start_seconds < cut:
            children[0].spans.append(span.model_copy(update={"end_seconds": min(cut, span.end_seconds)}))
        if cut < span.end_seconds:
            children[1].spans.append(span.model_copy(update={"start_seconds": max(cut, span.start_seconds)}))
    if any(not child.spans for child in children):
        raise ValueError("Both split games need a nonempty source span")
    if set(request.end_choices) - {e.id for e in parent.ends}:
        raise ValueError("An end assignment does not belong to this game")
    mappings, conflicts = [], []
    for end in sorted(parent.ends, key=lambda e: e.observed_index):
        allowed = []
        for side, child in zip(("earlier", "later"), children, strict=True):
            candidate = child.model_copy(update={"ends": [end]})
            try:
                validate_evidence(session, candidate)
            except ValueError:
                continue
            allowed.append(side)
        chosen = request.end_choices.get(end.id) or (allowed[0] if len(allowed) == 1 else None)
        if chosen not in allowed:
            conflicts.append(
                {
                    "key": end.id,
                    "kind": "split_end_assignment",
                    "official_number": end.official_number,
                    "observed_index": end.observed_index,
                    "options": allowed,
                    "reason": "Choose which game owns this end"
                    if allowed
                    else "End evidence crosses the split. Adjust the split or edit its evidence first.",
                }
            )
            continue
        child = children[("earlier", "later").index(chosen)]
        child.ends.append(_copy_end(end, len(child.ends) + 1))
        mappings.append(
            {
                "parent_game_id": parent.id,
                "parent_end_id": end.id,
                "successor_game_id": child.id,
                "successor_end_id": end.id,
                "action": "moved",
            }
        )
    return children, mappings, conflicts


def _merge(session, request, parents, identifier):
    games = [Game.model_validate(r.document) for r in parents]
    child = _draft(
        games[0].model_dump(),
        str(uuid5(NAMESPACE_URL, identifier + "merged")),
        request.label or "Merged game",
    )
    child.spans = []
    seen = set()
    for game in games:
        for span in game.spans:
            # Preserve actual intervals and relationships, including gaps and overlaps.
            key = digest(span.model_dump())
            if key not in seen:
                child.spans.append(span.model_copy(deep=True))
                seen.add(key)
    for field, fallback in [
        ("red_team", "Red · unidentified"),
        ("yellow_team", "Yellow · unidentified"),
        ("league", None),
    ]:
        values = {getattr(g, field) for g in games}
        setattr(child, field, next(iter(values)) if len(values) == 1 else fallback)
    child.note = "\n\n".join(f"{g.label}: {g.note}" for g in games if g.note)[:10000]
    groups = {}
    for game in games:
        for end in sorted(game.ends, key=lambda e: e.observed_index):
            key = str(end.official_number) if end.official_number is not None else f"{game.id}/{end.id}"
            groups.setdefault(key, []).append((game, end))
    if set(request.end_choices) - {key for key, entries in groups.items() if len(entries) > 1}:
        raise ValueError("An end choice does not identify a duplicate official end")
    mappings, conflicts = [], []
    for key, entries in groups.items():
        choices = {f"{g.id}/{e.id}": (g, e) for g, e in entries}
        chosen = request.end_choices.get(key) if len(entries) > 1 else next(iter(choices))
        if chosen not in choices:
            conflicts.append(
                {
                    "key": key,
                    "kind": "duplicate_official_end",
                    "official_number": entries[0][1].official_number,
                    "options": [
                        {"value": value, "parent_label": g.label, "end": e.model_dump()}
                        for value, (g, e) in choices.items()
                    ],
                    "reason": (
                        "Choose the end interpretation to carry forward; others remain in parent history"
                    ),
                }
            )
            continue
        winner = choices[chosen][1]
        if any(e.id == winner.id for e in child.ends):
            raise ValueError("Duplicate end IDs require correction before merging")
        child.ends.append(_copy_end(winner, len(child.ends) + 1))
        for game, end in entries:
            mappings.append(
                {
                    "parent_game_id": game.id,
                    "parent_end_id": end.id,
                    "successor_game_id": child.id,
                    "successor_end_id": winner.id,
                    "action": "moved" if f"{game.id}/{end.id}" == chosen else "superseded",
                }
            )
    # Known official numbers determine their relative order; unknown ends stay explicit at the end.
    child.ends.sort(key=lambda e: (e.official_number is None, e.official_number or e.observed_index))
    for i, end in enumerate(child.ends, 1):
        end.observed_index = i
    return [child], mappings, conflicts


def preview_in_session(session, request):
    parents = _parents(session, request)
    identifier = operation_id(request)
    children, mappings, conflicts = (_split if request.action == "split" else _merge)(
        session, request, parents, identifier
    )
    if not conflicts:
        for child in children:
            validated = Game.model_validate(child.model_dump())
            validate_evidence(session, validated)
    source_ids = {s["source_id"] for r in parents for s in r.document["spans"]}
    payload = {
        "id": identifier,
        "action": request.action,
        "request": request.model_dump(),
        "parents": [{"game": r.document, "revision": r.revision} for r in parents],
        "successors": [g.model_dump() for g in children] if not conflicts else [],
        "end_mappings": mappings if not conflicts else [],
        "conflicts": conflicts,
        "invalidated_results": [r.id for r in parents if r.document["result"]["outcome"] != "unknown"],
        "source_metadata_sha256": {
            sid: session.get(Source, sid).document.get("metadata_sha256") for sid in sorted(source_ids)
        },
        "notice": (
            "New game boundaries and carried scores require review. "
            "Previous interpretations and final results remain in history."
        ),
    }
    return {**payload, "preview_sha256": digest(payload) if not conflicts else None}


def preview_reconciliation(db, request):
    with db.snapshot_session() as session:
        return preview_in_session(session, request)


def present(row):
    return {
        "id": row.id,
        "revision": row.revision,
        "state": row.state,
        "created_at": row.created_at,
        **row.document,
    }


def commit_reconciliation(db, command):
    with db.snapshot_session(write=True) as session:
        existing = session.get(Reconciliation, operation_id(command.request))
        if existing:
            if existing.document["preview_sha256"] != command.preview_sha256:
                raise Conflict("This reconciliation was published with a different preview")
            return present(existing)
        plan = preview_in_session(session, command.request)
        if plan["conflicts"] or plan["preview_sha256"] != command.preview_sha256:
            raise Conflict("The split/merge preview changed; review a fresh preview before publishing")
        created = []
        for game in plan["successors"]:
            created.append(
                save_in_session(
                    session,
                    GameEdit(
                        expected_revision=0,
                        author=command.author,
                        reason=command.reason,
                        game=Game.model_validate(game),
                    ),
                    reconciliation=True,
                )
            )
        retired = []
        for parent in plan["parents"]:
            game = Game.model_validate(parent["game"])
            game.retired_into = [g["id"] for g in created]
            retired.append(
                save_in_session(
                    session,
                    GameEdit(
                        expected_revision=parent["revision"],
                        author=command.author,
                        reason=command.reason,
                        game=game,
                    ),
                    reconciliation=True,
                )
            )
        row = Reconciliation(
            id=plan["id"],
            document={
                **plan,
                "author": command.author,
                "reason": command.reason,
                "successor_revisions": {g["id"]: g["revision"] for g in created},
                "retired_revisions": {g["id"]: g["revision"] for g in retired},
            },
        )
        session.add(row)
        session.flush()
        return present(row)


def undo_reconciliation(db, identifier, command):
    with db.snapshot_session(write=True) as session:
        row = session.get(Reconciliation, identifier)
        if row is None or row.revision != command.expected_revision or row.state != "active":
            raise Conflict("This reconciliation changed or was already undone; reload its history")
        doc = row.document
        for game_id, revision in {**doc["retired_revisions"], **doc["successor_revisions"]}.items():
            game = session.get(GameRow, game_id)
            if game is None or game.revision != revision:
                raise Conflict(
                    "A related game has been edited or reconciled again. Undo would overwrite later work."
                )
        for parent in doc["parents"]:
            game = Game.model_validate(parent["game"])
            save_in_session(
                session,
                GameEdit(
                    expected_revision=doc["retired_revisions"][game.id],
                    author=command.author,
                    reason=command.reason,
                    game=game,
                ),
                reconciliation=True,
            )
        for game_id, revision in doc["successor_revisions"].items():
            game = Game.model_validate(session.get(GameRow, game_id).document)
            game.retired_into = list(doc["retired_revisions"])
            save_in_session(
                session,
                GameEdit(expected_revision=revision, author=command.author, reason=command.reason, game=game),
                reconciliation=True,
            )
        row.state = "undone"
        row.document = {**doc, "undo": {"author": command.author, "reason": command.reason, "at": now()}}
        session.flush()
        return present(row)


def reconciliations_in_session(session):
    return [present(r) for r in session.scalars(select(Reconciliation).order_by(Reconciliation.created_at))]
