"""Named people, team labels and operator links for anonymous game players.

Similarity only ranks suggestions. A player becomes a person only through an explicit,
revisioned operator link; unnamed players stay anonymous.
"""

from typing import Literal
from uuid import uuid4

import numpy as np
from pydantic import Field, model_validator
from sqlalchemy import String, select
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.orm.exc import StaleDataError

from hogline.adapters.sources import digest
from hogline.analytics.biometrics import array_json, delete_archive, read_archive
from hogline.db import GameLineup, LineupLink, LineupPerson, LineupRevision, LineupSegment, LineupSeparation
from hogline.domain.catalog import Conflict
from hogline.domain.models import Model

POSITIONS = ("lead", "second", "third", "skip", "alternate")
# Template-to-template cosine similarity. ArcFace values come from the October 2026
# experiment: simultaneous non-adjacent games never exceeded 0.73 between identities of
# 15+ faces. SFace values are uncalibrated and deliberately stricter.
SUGGESTION = {"arcface-r50": {"likely": 0.75, "possible": 0.55}, "sface": {"likely": 0.8, "possible": 0.62}}
LIKELY_MIN_FACES = 15
MIN_SUGGESTION_FACES = 5


class Review(Model):
    author: str = Field(default="Local operator", min_length=1, max_length=120)
    reason: str = Field(default="Lineup gallery edit", min_length=1, max_length=2000)


class PersonEdit(Review):
    expected_revision: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=160)
    team: str | None = Field(default=None, max_length=120)
    note: str = Field(default="", max_length=2000)


class NewPerson(Model):
    name: str = Field(min_length=1, max_length=160)
    team: str | None = Field(default=None, max_length=120)


class PlayerLink(Review):
    person_id: str | None = None
    new_person: NewPerson | None = None
    position: Literal["lead", "second", "third", "skip", "alternate"] | None = None
    expected_revision: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def one_target(self):
        if (self.person_id is None) == (self.new_person is None):
            raise ValueError("Link a player to one existing person or one new person")
        return self


def clean(text):
    return " ".join(text.split()) if text else None


def checked(row):
    if digest(row.document) != row.sha256:
        raise ValueError("Lineup record checksum mismatch")
    return {"id": row.id, **({"revision": row.revision} if hasattr(row, "revision") else {}), **row.document}


def save_revision(session, kind, row, author, reason, before):
    """Append-only history; numbering continues across delete and re-create of an entity."""
    document = {"after": checked(row) if row is not None else None, "before": before}
    entity = row.id if row is not None else before["id"]
    session.flush()
    previous = session.scalar(
        select(LineupRevision.number)
        .where(LineupRevision.kind == kind, LineupRevision.entity_id == entity)
        .order_by(LineupRevision.number.desc())
    )
    session.add(
        LineupRevision(
            kind=kind,
            entity_id=entity,
            number=(previous or 0) + 1,
            author=author,
            reason=reason,
            sha256=digest(document),
            document=document,
        )
    )


def touch(session, row, document):
    row.document, row.sha256 = document, digest(document)
    flag_modified(row, "document")
    try:
        session.flush()
    except StaleDataError as exc:
        raise Conflict("Lineup names changed in another edit; reload before saving") from exc


def lineup_player(session, player_id, lineup_id=None):
    """Find a game player; a known lineup ID avoids reading every lineup."""
    if lineup_id is not None:
        row = session.get(GameLineup, lineup_id)
        rows = [row] if row is not None else []
    else:
        text = GameLineup.document.cast(String)
        rows = session.scalars(select(GameLineup).where(text.contains(player_id)))
    for row in rows:
        lineup = checked(row)
        for player in lineup["players"]:
            if player["id"] == player_id:
                return lineup, player
    raise ValueError("Lineup player not found")


def create_person(session, name, team, author, reason):
    document = {"name": clean(name), "team": clean(team), "note": ""}
    if not document["name"]:
        raise ValueError("A person needs a name")
    row = LineupPerson(id=str(uuid4()), sha256=digest(document), document=document)
    session.add(row)
    session.flush()
    save_revision(session, "person", row, author, reason, None)
    return row


def edit_person(db, person_id, edit: PersonEdit):
    with db.snapshot_session(write=True) as session:
        row = session.get(LineupPerson, person_id)
        if row is None:
            raise ValueError("Person not found")
        before = checked(row)
        if row.revision != edit.expected_revision:
            raise Conflict("Person changed; reload before saving")
        name = clean(edit.name)
        if not name:
            raise ValueError("A person needs a name")
        touch(session, row, {"name": name, "team": clean(edit.team), "note": edit.note})
        save_revision(session, "person", row, edit.author, edit.reason, before)
        return checked(row)


def link_player(db, player_id, edit: PlayerLink, lineup_id=None):
    with db.snapshot_session(write=True) as session:
        lineup, _ = lineup_player(session, player_id, lineup_id)
        row = session.get(LineupLink, player_id)
        if (row.revision if row else None) != edit.expected_revision:
            raise Conflict("This player's name changed; reload before saving")
        person = resolve_person(session, edit)
        return link_in_session(
            session, lineup["id"], player_id, person, edit.position, edit.author, edit.reason
        )


def resolve_person(session, edit):
    """The existing person an edit names, or a newly created one."""
    if edit.person_id:
        person = session.get(LineupPerson, edit.person_id)
        if person is None:
            raise ValueError("Person not found")
        checked(person)
        return person
    return create_person(session, edit.new_person.name, edit.new_person.team, edit.author, edit.reason)


def link_in_session(session, lineup_id, player_id, person, position, author, reason):
    """Create or replace one player's operator link; callers check revisions first."""
    from hogline.domain.face_groups import check_group_name

    check_group_name(session, player_id, person.id)
    row = session.get(LineupLink, player_id)
    document = {
        "lineup_id": lineup_id,
        "player_id": player_id,
        "person_id": person.id,
        "position": position,
        "basis": "operator_confirmed",
    }
    if row is None:
        row = LineupLink(
            id=player_id,
            lineup_id=lineup_id,
            person_id=person.id,
            sha256=digest(document),
            document=document,
        )
        session.add(row)
        session.flush()
        save_revision(session, "link", row, author, reason, None)
    else:
        before = checked(row)
        row.person_id = person.id
        touch(session, row, document)
        save_revision(session, "link", row, author, reason, before)
    return checked(row)


def remove_separations(session, review, *, player_ids=(), person_id=None):
    """Delete separations that mention removed players or a forgotten person (with history)."""
    player_ids = set(player_ids)
    removed = 0
    for row in session.scalars(select(LineupSeparation)):
        doc = row.document
        other = doc.get("other", {})
        if (
            doc["player_id"] in player_ids
            or other.get("player_id") in player_ids
            or (person_id is not None and other.get("person_id") == person_id)
        ):
            before = checked(row)
            session.delete(row)
            session.flush()
            save_revision(session, "separation", None, review.author, review.reason, before)
            removed += 1
    return removed


def unlink_player(db, player_id, expected_revision, review: Review):
    with db.snapshot_session(write=True) as session:
        row = session.get(LineupLink, player_id)
        if row is None:
            raise ValueError("This player has no name to remove")
        if row.revision != expected_revision:
            raise Conflict("This player's name changed; reload before removing it")
        before = checked(row)
        session.delete(row)
        session.flush()
        save_revision(session, "link", None, review.author, review.reason, before)
        return {"id": player_id, "removed": True}


def delete_person(db, person_id, expected_revision, review: Review):
    with db.snapshot_session(write=True) as session:
        row = session.get(LineupPerson, person_id)
        if row is None:
            raise ValueError("Person not found")
        if row.revision != expected_revision:
            raise Conflict("Person changed; reload before deleting")
        for link in session.scalars(select(LineupLink).where(LineupLink.person_id == person_id)):
            before = checked(link)
            session.delete(link)
            session.flush()
            save_revision(session, "link", None, review.author, review.reason, before)
        remove_separations(session, review, person_id=person_id)
        before = checked(row)
        session.delete(row)
        session.flush()
        save_revision(session, "person", None, review.author, review.reason, before)
        return {"id": person_id, "removed": True}


def delete_lineup(db, lineup_id, review: Review):
    """Remove a game's names, groups and biometric files. Job history is kept."""
    with db.snapshot_session(write=True) as session:
        removed = delete_lineup_in_session(session, lineup_id, review)
    for archive in removed:
        delete_archive(db.settings.data_dir, archive)
    return {"id": lineup_id, "removed": True, "biometric_archives_removed": len(removed)}


def delete_lineup_in_session(session, lineup_id, review):
    row = session.get(GameLineup, lineup_id)
    if row is None:
        raise ValueError("Lineup not found")
    lineup = checked(row)
    for link in session.scalars(select(LineupLink).where(LineupLink.lineup_id == lineup_id)):
        before = checked(link)
        session.delete(link)
        session.flush()
        save_revision(session, "link", None, review.author, review.reason, before)
    remove_separations(session, review, player_ids=[p["id"] for p in lineup["players"]])
    from hogline.domain.face_groups import remove_grouped_players

    remove_grouped_players(session, [p["id"] for p in lineup["players"]], review)
    others = {
        s["id"]
        for other in session.scalars(select(GameLineup).where(GameLineup.id != lineup_id))
        for s in other.document["segments"]
    }
    removed = [lineup["templates"]]
    for ref in lineup["segments"]:
        segment = session.get(LineupSegment, ref["id"])
        if segment is not None and ref["id"] not in others:
            removed.append(segment.document["archive"])
            session.delete(segment)
    session.delete(row)
    session.flush()
    save_revision(
        session,
        "lineup",
        None,
        review.author,
        review.reason,
        {"id": lineup_id, "source_id": lineup["source_id"], "biometrics_deleted": True},
    )
    return removed


def _templates(data_dir, lineups, cache):
    out = {}
    for lineup in lineups:
        ref = lineup["templates"]
        key = (ref["path"], ref["sha256"])
        if key not in cache:
            try:
                arrays = read_archive(data_dir, ref)
                cache[key] = dict(zip(array_json(arrays["player_ids"]), arrays["templates"], strict=True))
            except ValueError:
                cache[key] = {}
        out.update({pid: (lineup["embedder"], vector) for pid, vector in cache[key].items()})
    return out


_CACHE = {}


def pooled_person_templates(persons, templates):
    """One template per (person, embedder) from the person's confirmed players."""
    result = {}
    for person in persons.values():
        by_model = {}
        for pid in person["player_ids"]:
            if pid in templates:
                model, vector = templates[pid]
                by_model.setdefault(model, []).append(vector)
        for model, vectors in by_model.items():
            mean = np.mean(vectors, axis=0)
            result[(person["id"], model)] = mean / max(np.linalg.norm(mean), 1e-12)
    return result


def annotate_lineup(lineup, links, persons, templates, person_templates):
    """Attach each player's current name and up to three named-person suggestions."""
    lineup["named_people"] = 0
    for player in lineup["players"]:
        link = links.get(player["id"])
        player["link"] = (
            {
                "revision": link["revision"],
                "person_id": link["person_id"],
                "position": link["position"],
                "name": persons[link["person_id"]]["name"],
                "team": persons[link["person_id"]]["team"],
            }
            if link
            else None
        )
        lineup["named_people"] += bool(link)
        player["suggestions"] = []
        if link or player["id"] not in templates or player["faces"] < MIN_SUGGESTION_FACES:
            continue
        model, vector = templates[player["id"]]
        limits = SUGGESTION.get(model, SUGGESTION["sface"])
        ranked = []
        for (person_id, person_model), reference in person_templates.items():
            if person_model != model:
                continue
            similarity = float(vector @ reference)
            if similarity >= limits["possible"]:
                likely = similarity >= limits["likely"] and player["faces"] >= LIKELY_MIN_FACES
                ranked.append(
                    {
                        "person_id": person_id,
                        "name": persons[person_id]["name"],
                        "team": persons[person_id]["team"],
                        "similarity": round(similarity, 3),
                        "strength": "likely" if likely else "possible",
                    }
                )
        player["suggestions"] = sorted(ranked, key=lambda s: -s["similarity"])[:3]
    return lineup


def people_and_links(session):
    persons = {r.id: checked(r) for r in session.scalars(select(LineupPerson))}
    links = {r.id: checked(r) for r in session.scalars(select(LineupLink))}
    for person in persons.values():
        person["player_ids"] = [pid for pid, link in links.items() if link["person_id"] == person["id"]]
        person["lineup_ids"] = sorted(
            {link["lineup_id"] for link in links.values() if link["person_id"] == person["id"]}
        )
    return persons, links


def sorted_people(persons):
    return sorted(persons.values(), key=lambda p: ((p["team"] or "~").lower(), p["name"].lower()))


def lineup_detail(session, data_dir, lineup_id):
    """One game's players with names and suggestions, without reading every other game."""
    row = session.get(GameLineup, lineup_id)
    if row is None:
        raise ValueError("Lineup not found")
    lineup = checked(row)
    persons, links = people_and_links(session)
    needed = {lineup_id: lineup}
    for link in links.values():
        if link["lineup_id"] not in needed:
            other = session.get(GameLineup, link["lineup_id"])
            if other is not None:
                needed[other.id] = checked(other)
    templates = _templates(data_dir, needed.values(), _CACHE)
    annotate_lineup(lineup, links, persons, templates, pooled_person_templates(persons, templates))
    return {"lineup": lineup, "people": sorted_people(persons), "positions": list(POSITIONS)}


def lineups_in_session(session, data_dir, history=False):
    lineups = sorted(
        (checked(r) for r in session.scalars(select(GameLineup))),
        key=lambda x: (x.get("title_date") or "", x.get("sheet") or 0, x["start_seconds"]),
    )
    persons, links = people_and_links(session)
    order = {lineup["id"]: n for n, lineup in enumerate(lineups)}
    for person in persons.values():
        person["lineup_ids"] = sorted(
            (lid for lid in person["lineup_ids"] if lid in order), key=lambda lid: order[lid]
        )
    templates = _templates(data_dir, lineups, _CACHE)
    # Suggestions compare like with like: one embedder's templates with the same embedder's.
    person_templates = pooled_person_templates(persons, templates)
    for lineup in lineups:
        annotate_lineup(lineup, links, persons, templates, person_templates)
    teams = {}
    for person in persons.values():
        if person["team"]:
            teams.setdefault(person["team"], []).append(person)
    team_games = []
    for team, members in sorted(teams.items()):
        games = []
        for lineup in lineups:
            present = sorted(
                {
                    persons[p["link"]["person_id"]]["name"]
                    for p in lineup["players"]
                    if p["link"] and p["link"]["team"] == team
                }
            )
            if present:
                games.append(
                    {
                        "lineup_id": lineup["id"],
                        "title": lineup.get("title"),
                        "title_date": lineup.get("title_date"),
                        "sheet": lineup.get("sheet"),
                        "start_seconds": lineup["start_seconds"],
                        "members_present": present,
                        "confidence": "team_game" if len(present) >= 2 else "one_member_only",
                    }
                )
        team_games.append({"team": team, "people": sorted(m["name"] for m in members), "games": games})
    result = {
        "lineups": lineups,
        "people": sorted_people(persons),
        "teams": team_games,
        "positions": list(POSITIONS),
    }
    if history:
        result["history"] = [
            {
                "kind": r.kind,
                "entity_id": r.entity_id,
                "number": r.number,
                "author": r.author,
                "reason": r.reason,
                "sha256": r.sha256,
                "created_at": r.created_at,
                **r.document,
            }
            for r in session.scalars(select(LineupRevision).order_by(LineupRevision.created_at))
        ]
    return result


def more_like_person(session, data_dir, person_id, limit=24):
    """Rank unnamed players in every lineup by similarity to one named person."""
    view = lineups_in_session(session, data_dir)
    person = next((p for p in view["people"] if p["id"] == person_id), None)
    if person is None:
        raise ValueError("Person not found")
    found = []
    for lineup in view["lineups"]:
        for player in lineup["players"]:
            match = next((s for s in player["suggestions"] if s["person_id"] == person_id), None)
            if match:
                found.append(
                    {"lineup_id": lineup["id"], "title": lineup.get("title"), "player": player, **match}
                )
    return sorted(found, key=lambda f: -f["similarity"])[:limit]
