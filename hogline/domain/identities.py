"""Browse people across games: cross-game face identities over game lineups.

An identity is a proposal: game players whose face templates match across games, kept
apart where they shared a game, where they carry two different names, or where an operator
said "not this person". Names attach only through operator links; naming an identity
records one link per game player the operator saw.
"""

import threading

import numpy as np
from pydantic import Field, model_validator
from sqlalchemy import select

from hogline.adapters.sources import digest
from hogline.analytics.biometrics import array_json, read_archive
from hogline.db import GameLineup, LineupGrouping, LineupLink, LineupPerson, LineupSegment, LineupSeparation
from hogline.domain.catalog import Conflict
from hogline.domain.face_groups import components, confirmed_components
from hogline.domain.lineups import (
    LIKELY_MIN_FACES,
    SUGGESTION,
    NewPerson,
    Review,
    checked,
    link_in_session,
    resolve_person,
    save_revision,
)
from hogline.reconstruction.identities import VERSION, link_identities, pooled
from hogline.reconstruction.lineups import portrait_face

MIN_IDENTITY_FACES = LIKELY_MIN_FACES
MAX_POSSIBLE = 12
PLAYER_FIELDS = (
    "id",
    "number",
    "faces",
    "role",
    "first_seconds",
    "last_seconds",
    "median_eye_px",
    "views",
)


class NameIdentity(Review):
    """Name the game players an operator reviewed as one person."""

    player_ids: list[str] = Field(min_length=1, max_length=2000)
    person_id: str | None = None
    new_person: NewPerson | None = None

    @model_validator(mode="after")
    def one_target(self):
        if (self.person_id is None) == (self.new_person is None):
            raise ValueError("Name these players as one existing person or one new person")
        return self


class Separate(Review):
    """Keep one game player apart from another player or from a named person."""

    from_player_id: str | None = None
    from_person_id: str | None = None

    @model_validator(mode="after")
    def one_target(self):
        if (self.from_player_id is None) == (self.from_person_id is None):
            raise ValueError("Separate from one player or one named person")
        return self


class GroupFaces(Review):
    player_ids: list[str] = Field(min_length=2, max_length=2000)
    expected_fingerprint: str = Field(min_length=64, max_length=64)


_LOCK = threading.Lock()
_COMPACT = {}  # (lineup id, sha256) -> compact lineup with eligible-player templates
_INDEX = {}  # fingerprint -> index


def _portrait(lineup_id, player):
    face = player.get("portrait") or (player["thumbnails"][0] if player.get("thumbnails") else None)
    if face is None:
        return None
    return {
        "lineup_id": lineup_id,
        "player_id": player["id"],
        "segment_id": face["segment_id"],
        "thumb": face["thumb"],
        "eye_px": face.get("eye_px", player.get("median_eye_px")),
        "chosen": "clear_face" if player.get("portrait") else "largest_face",
    }


def legacy_portrait(session, data_dir, player, reference, archives):
    """Portrait for a lineup grouped before portraits existed: the best of its stored faces.

    Uses the same rule as new lineups, over the player's representative thumbnails only.
    """
    faces, vectors, images = [], [], []
    for ref in player.get("thumbnails", []):
        if ref["segment_id"] not in archives:
            row = session.get(LineupSegment, ref["segment_id"])
            try:
                arrays = read_archive(data_dir, checked(row)["archive"]) if row is not None else None
            except ValueError:
                arrays = None
            archives[ref["segment_id"]] = (
                None
                if arrays is None
                else {
                    "by_thumb": {
                        f["thumb"]: (f, v)
                        for f, v in zip(array_json(arrays["meta"]), arrays["embeddings"], strict=True)
                        if f["thumb"] >= 0
                    },
                    "thumbnails": arrays["thumbnails"],
                }
            )
        archive = archives[ref["segment_id"]]
        if archive is None or ref["thumb"] not in archive["by_thumb"]:
            continue
        face, vector = archive["by_thumb"][ref["thumb"]]
        faces.append({**face, "thumb": ref["thumb"], "segment_id": ref["segment_id"]})
        vectors.append(np.asarray(vector, dtype=np.float32) / max(float(np.linalg.norm(vector)), 1e-12))
        images.append(archive["thumbnails"][ref["thumb"]])
    if not faces:
        return None
    chosen = portrait_face(range(len(faces)), faces, np.stack(vectors), reference, lambda i: images[i])
    return None if chosen is None else {**faces[chosen[0]], "template_similarity": chosen[1]}


def _compact(session, data_dir, row):
    """Small per-lineup projection, cached by checksum; full documents stay in the database."""
    key = (row.id, row.sha256)
    if key in _COMPACT:
        return _COMPACT[key]
    lineup = checked(row)
    archives = {}
    try:
        arrays = read_archive(data_dir, lineup["templates"])
        vectors = dict(zip(array_json(arrays["player_ids"]), arrays["templates"], strict=True))
        template_error = None
    except ValueError as exc:
        vectors, template_error = {}, str(exc)
    players = {}
    for player in lineup["players"]:
        # Keep even tiny groups available for explicit operator grouping.
        compact = {k: player.get(k) for k in PLAYER_FIELDS}
        compact["lineup_id"] = lineup["id"]
        compact["model"] = lineup["embedder"]
        vector = vectors.get(player["id"]) if player["faces"] >= MIN_IDENTITY_FACES else None
        compact["vector"] = None if vector is None else np.asarray(vector, dtype=np.float32)
        compact["portrait"] = _portrait(lineup["id"], player)
        if not player.get("portrait") and compact["vector"] is not None:
            better = legacy_portrait(session, data_dir, player, compact["vector"], archives)
            if better is not None:
                compact["portrait"] = {
                    **compact["portrait"],
                    "segment_id": better["segment_id"],
                    "thumb": better["thumb"],
                    "eye_px": better["eye_px"],
                    "chosen": "clear_face_of_stored",
                }
        compact["watch_url"] = (player.get("portrait") or {}).get("youtube_url") or player["best_face"].get(
            "youtube_url"
        )
        players[player["id"]] = compact
    out = {
        "id": lineup["id"],
        "source_id": lineup["source_id"],
        "title": lineup.get("title"),
        "title_date": lineup.get("title_date"),
        "sheet": lineup.get("sheet"),
        "start_seconds": lineup["start_seconds"],
        "end_seconds": lineup["end_seconds"],
        "embedder": lineup["embedder"],
        "automatic": bool(lineup["request"].get("automatic")),
        "face_count": lineup["face_count"],
        "players": players,
        "template_error": template_error,
        "created_at": lineup["created_at"],
    }
    _COMPACT[key] = out
    return out


def lineup_summary(compact):
    return {
        k: compact[k]
        for k in (
            "id",
            "source_id",
            "title",
            "title_date",
            "sheet",
            "start_seconds",
            "end_seconds",
            "embedder",
            "automatic",
        )
    }


def fingerprint(session):
    return digest(
        {
            "version": VERSION,
            "lineups": sorted(
                [r.id, r.sha256] for r in session.execute(select(GameLineup.id, GameLineup.sha256))
            ),
            "links": sorted(
                [r.id, r.revision, r.person_id]
                for r in session.execute(select(LineupLink.id, LineupLink.revision, LineupLink.person_id))
            ),
            "persons": sorted(
                [r.id, r.revision] for r in session.execute(select(LineupPerson.id, LineupPerson.revision))
            ),
            "separations": sorted(
                [r.id, r.sha256]
                for r in session.execute(select(LineupSeparation.id, LineupSeparation.sha256))
            ),
            "groupings": sorted(
                [r.id, r.sha256] for r in session.execute(select(LineupGrouping.id, LineupGrouping.sha256))
            ),
        }
    )


def face_index(session, data_dir):
    """Cross-game identities for the current lineups, names and separations (cached)."""
    key = fingerprint(session)
    with _LOCK:
        if key in _INDEX:
            return _INDEX[key]
    index = build_index(session, data_dir)
    index["fingerprint"] = key
    with _LOCK:
        _INDEX.clear()
        _INDEX[key] = index
        live = {(r.id, r.sha256) for r in session.execute(select(GameLineup.id, GameLineup.sha256))}
        for stale in [k for k in _COMPACT if k not in live]:
            del _COMPACT[stale]
    return index


def build_index(session, data_dir):
    lineups = {}
    for row in session.scalars(select(GameLineup)):
        lineups[row.id] = _compact(session, data_dir, row)
    persons = {r.id: checked(r) for r in session.scalars(select(LineupPerson))}
    links = {r.id: checked(r) for r in session.scalars(select(LineupLink))}
    separations = [checked(r) for r in session.scalars(select(LineupSeparation))]
    groupings = [checked(r) for r in session.scalars(select(LineupGrouping))]
    players = {pid: player for lineup in lineups.values() for pid, player in lineup["players"].items()}
    roots = confirmed_components(players, groupings, links)
    manual = {pid for group in groupings for pid in group["player_ids"] if pid in players}
    component_names = {roots[pid]: link["person_id"] for pid, link in links.items() if pid in roots}
    component_games = {}
    for pid, root in roots.items():
        component_games.setdefault(root, set()).add(players[pid]["lineup_id"])
    named_players = {
        pid: {
            "person_id": link["person_id"],
            "name": persons[link["person_id"]]["name"],
            "team": persons[link["person_id"]]["team"],
            "position": link["position"],
            "revision": link["revision"],
        }
        for pid, link in links.items()
        if pid in players
    }

    def eligible(pid):
        player = players[pid]
        return player["vector"] is not None and (
            pid in named_players or pid in manual or player["role"] == "player"
        )

    identities = []
    for model in sorted({p["model"] for p in players.values()}):
        nodes, node_of = [], {}
        named, by_component = {}, {}
        for pid, player in sorted(players.items(), key=lambda kv: kv[0]):
            if player["model"] != model or not eligible(pid):
                continue
            root = roots[pid]
            person_id = component_names.get(root)
            if root in by_component:
                node = nodes[by_component[root]]
                node["players"].append(pid)
                node_of[pid] = by_component[root]
                continue
            node = {"players": [pid], "person_id": person_id, "keys": set(component_games[root])}
            if person_id is not None:
                node["keys"].add("named")
                named[person_id] = len(nodes)
            node_of[pid] = len(nodes)
            by_component[root] = len(nodes)
            nodes.append(node)
        # Constraints follow manually grouped players even across embedders / small groups.
        for pid, root in roots.items():
            if root in by_component:
                node_of[pid] = by_component[root]
        if not nodes:
            continue
        for separation in separations:
            a = node_of.get(separation["player_id"])
            other = separation["other"]
            b = (
                node_of.get(other["player_id"])
                if other.get("player_id")
                else named.get(other.get("person_id"))
            )
            if a is not None and b is not None and a != b:
                nodes[a]["keys"].add(f"separation:{separation['id']}")
                nodes[b]["keys"].add(f"separation:{separation['id']}")
        weights = [sum(players[p]["faces"] for p in node["players"]) for node in nodes]
        vectors = np.stack(
            [
                pooled(
                    [players[p]["vector"] for p in node["players"]],
                    [players[p]["faces"] for p in node["players"]],
                )
                for node in nodes
            ]
        )
        limits = SUGGESTION.get(model, SUGGESTION["sface"])
        labels = link_identities(vectors, [n["keys"] for n in nodes], weights, limits["likely"])
        for label in range(int(labels.max()) + 1):
            members = np.flatnonzero(labels == label).tolist()
            pids = [p for m in members for p in nodes[m]["players"]]
            person_id = next((nodes[m]["person_id"] for m in members if nodes[m]["person_id"]), None)
            identities.append(
                {
                    "model": model,
                    "person_id": person_id,
                    "players": pids,
                    "vector": pooled([vectors[m] for m in members], [weights[m] for m in members]),
                }
            )
    # Named players too small to compare still belong with the rest of their person's games.
    placed = {pid for identity in identities for pid in identity["players"]}
    for pid in sorted(set(named_players) | manual):
        if pid not in placed:
            identities.append(
                {
                    "model": None,
                    "person_id": component_names.get(roots[pid]),
                    "players": [pid],
                    "vector": None,
                }
            )
    # Join confirmed components across embedders, including groups too small to compare.
    placed = {pid for identity in identities for pid in identity["players"]}
    confirmed = {}
    for pid in placed:
        confirmed.setdefault(roots[pid], []).append(pid)
    final_roots = components(placed, [i["players"] for i in identities] + list(confirmed.values()))
    merged_by_root = {}
    for pid in sorted(placed):
        merged_by_root.setdefault(final_roots[pid], []).append(pid)
    merged = []
    for pids in merged_by_root.values():
        by_model = {}
        for pid in pids:
            if players[pid]["vector"] is not None:
                by_model.setdefault(players[pid]["model"], []).append(pid)
        merged.append(
            {
                "person_id": next(
                    (component_names[roots[p]] for p in pids if roots[p] in component_names), None
                ),
                "players": pids,
                "vectors": {
                    model: pooled(
                        [players[p]["vector"] for p in members], [players[p]["faces"] for p in members]
                    )
                    for model, members in by_model.items()
                },
            }
        )
    by_player = {}
    for number, identity in enumerate(merged):
        identity["players"].sort(
            key=lambda pid: (
                lineups[players[pid]["lineup_id"]]["title_date"] or "",
                lineups[players[pid]["lineup_id"]]["sheet"] or 0,
                lineups[players[pid]["lineup_id"]]["start_seconds"],
                players[pid]["number"],
            )
        )
        for pid in identity["players"]:
            by_player[pid] = number
        lineup_ids = list(dict.fromkeys(players[pid]["lineup_id"] for pid in identity["players"]))
        dates = [lineups[lid]["title_date"] for lid in lineup_ids if lineups[lid]["title_date"]]
        anchor = max(
            identity["players"],
            key=lambda pid: (
                players[pid]["portrait"] is not None,
                (players[pid]["portrait"] or {}).get("eye_px") or 0,
                players[pid]["faces"],
            ),
        )
        person = persons.get(identity["person_id"]) if identity["person_id"] else None
        identity.update(
            {
                "id": anchor,
                "person": None
                if person is None
                else {
                    "id": person["id"],
                    "name": person["name"],
                    "team": person["team"],
                    "revision": person["revision"],
                },
                "lineup_ids": lineup_ids,
                "games": len(lineup_ids),
                "recordings": len({lineups[lid]["source_id"] for lid in lineup_ids}),
                "faces": sum(players[pid]["faces"] for pid in identity["players"]),
                "first_date": min(dates) if dates else None,
                "last_date": max(dates) if dates else None,
                "portrait": players[anchor]["portrait"],
            }
        )
    return {
        "identities": merged,
        "by_player": by_player,
        "players": players,
        "named": named_players,
        "links": links,
        "lineups": lineups,
        "persons": persons,
        "separations": separations,
        "groupings": groupings,
        "confirmed_roots": roots,
    }


def identity_summary(identity):
    return {
        k: identity[k]
        for k in ("id", "person", "games", "recordings", "faces", "first_date", "last_date", "portrait")
    }


def faces_overview(session, data_dir, *, query="", named=None, min_games=1, offset=0, limit=200):
    index = face_index(session, data_dir)
    query = query.strip().lower()
    selected = [
        identity
        for identity in index["identities"]
        if identity["games"] >= min_games
        and (named is None or (identity["person"] is not None) == named)
        and (
            not query
            or (
                identity["person"]
                and query in f"{identity['person']['name']} {identity['person']['team'] or ''}".lower()
            )
        )
    ]
    selected.sort(key=lambda i: (-i["games"], i["person"] is None, -i["faces"], i["id"]))
    return {
        "fingerprint": index["fingerprint"],
        "total": len(selected),
        "identities": [identity_summary(i) for i in selected[offset : offset + limit]],
        "counts": {
            "identities": len(index["identities"]),
            "named": sum(i["person"] is not None for i in index["identities"]),
            "in_two_or_more_games": sum(i["games"] >= 2 for i in index["identities"]),
            "lineups": len(index["lineups"]),
            "players_considered": len(index["by_player"]),
        },
        "method": {
            "version": VERSION,
            "player_min_faces": MIN_IDENTITY_FACES,
            "link_similarity": {model: limits["likely"] for model, limits in SUGGESTION.items()},
        },
    }


def appearance(index, pid, vector=None):
    player = index["players"][pid]
    lineup = index["lineups"][player["lineup_id"]]
    out = {k: v for k, v in player.items() if k != "vector"}
    out["link"] = index["named"].get(pid)
    out["lineup"] = lineup_summary(lineup)
    if vector is not None and player["vector"] is not None:
        out["similarity"] = round(float(player["vector"] @ vector), 3)
    return out


def _identity_for(index, player_id):
    if player_id not in index["players"]:
        raise ValueError("This face group is too small to compare across games")
    number = index["by_player"].get(player_id)
    if number is not None:
        return index["identities"][number]
    # Groups below the identity floor still open as a one-game view.
    player = index["players"][player_id]
    return {
        "id": player_id,
        "person_id": None,
        "person": None,
        "players": [player_id],
        "vectors": {} if player["vector"] is None else {player["model"]: player["vector"]},
        "lineup_ids": [player["lineup_id"]],
        "games": 1,
        "recordings": 1,
        "faces": player["faces"],
        "first_date": index["lineups"][player["lineup_id"]]["title_date"],
        "last_date": index["lineups"][player["lineup_id"]]["title_date"],
        "portrait": player["portrait"],
        "below_identity_floor": True,
    }


def possible_players(index, identity):
    """Unnamed players elsewhere that resemble this identity, below the automatic link."""
    in_games = set(identity["lineup_ids"])
    members = set(identity["players"])
    separated = set()
    for separation in index["separations"]:
        other = separation["other"]
        if separation["player_id"] in members and other.get("player_id"):
            separated.add(other["player_id"])
        if other.get("player_id") in members or (
            identity["person_id"] and other.get("person_id") == identity["person_id"]
        ):
            separated.add(separation["player_id"])
    found = []
    for pid, player in index["players"].items():
        vector = identity["vectors"].get(player["model"])
        if (
            vector is None
            or player["vector"] is None
            or pid in members
            or pid in separated
            or player["lineup_id"] in in_games
            or pid in index["named"]
            or player["role"] == "other_sheet"
        ):
            continue
        similarity = float(player["vector"] @ vector)
        if similarity >= SUGGESTION.get(player["model"], SUGGESTION["sface"])["possible"]:
            found.append((similarity, pid))
    found.sort(key=lambda f: (-f[0], f[1]))
    # At most one candidate per game: the closest.
    seen, out = set(), []
    for _, pid in found:
        lineup_id = index["players"][pid]["lineup_id"]
        if lineup_id in seen:
            continue
        seen.add(lineup_id)
        out.append(pid)
        if len(out) >= MAX_POSSIBLE:
            break
    return out


def face_detail(session, data_dir, player_id):
    index = face_index(session, data_dir)
    identity = _identity_for(index, player_id)
    vector_for = identity["vectors"]
    members = set(identity["players"])
    separations = [
        {
            "id": s["id"],
            "player": appearance(index, s["player_id"]) if s["player_id"] in index["players"] else None,
            "other": s["other"],
        }
        for s in index["separations"]
        if s["player_id"] in members
        or s["other"].get("player_id") in members
        or (identity["person_id"] and s["other"].get("person_id") == identity["person_id"])
    ]
    return {
        **identity_summary(identity),
        "fingerprint": index["fingerprint"],
        "groupings": [g for g in index["groupings"] if members.intersection(g["player_ids"])],
        "below_identity_floor": identity.get("below_identity_floor", False),
        "appearances": [
            appearance(index, pid, vector_for.get(index["players"][pid]["model"]))
            for pid in identity["players"]
        ],
        "possible": [
            appearance(index, pid, vector_for.get(index["players"][pid]["model"]))
            for pid in possible_players(index, identity)
        ],
        "separations": separations,
    }


def source_faces(session, data_dir, source_id):
    """People seen in one recording's lineups, each with how many games they appear in."""
    index = face_index(session, data_dir)
    lineups = sorted(
        (lineup for lineup in index["lineups"].values() if lineup["source_id"] == source_id),
        key=lambda x: x["start_seconds"],
    )
    games = []
    for lineup in lineups:
        people = []
        for pid, player in lineup["players"].items():
            link = index["named"].get(pid)
            if player["role"] != "player" and link is None:
                continue
            number = index["by_player"].get(pid)
            identity = index["identities"][number] if number is not None else None
            people.append(
                {
                    "player_id": pid,
                    "identity_id": identity["id"] if identity else pid,
                    "faces": player["faces"],
                    "link": link,
                    "person": identity["person"] if identity else None,
                    "games": identity["games"] if identity else 1,
                    "portrait": player["portrait"],
                }
            )
        people.sort(key=lambda x: (x["link"] is None, -x["faces"]))
        games.append({**lineup_summary(lineup), "people": people})
    return games


def names_by_source(session, data_dir):
    index = face_index(session, data_dir)
    names = {}
    for pid, link in index["named"].items():
        source_id = index["lineups"][index["players"][pid]["lineup_id"]]["source_id"]
        names.setdefault(source_id, set()).update(filter(None, (link["name"], link["team"])))
    return {source_id: sorted(values) for source_id, values in names.items()}


def name_identity(db, player_id, body: NameIdentity):
    """Link each reviewed player of one identity (and checked possible matches) to one person."""
    with db.snapshot_session(write=True) as session:
        index = face_index(session, db.settings.data_dir)
        identity = _identity_for(index, player_id)
        allowed = set(identity["players"]) | set(possible_players(index, identity))
        unknown = [pid for pid in body.player_ids if pid not in allowed]
        if unknown:
            raise Conflict("These faces changed since you looked; reload before naming them")
        person = resolve_person(session, body)
        named, unchanged, skipped = [], [], []
        for pid in dict.fromkeys(body.player_ids):
            player = index["players"][pid]
            current = session.get(LineupLink, pid)
            if current is not None:
                (unchanged if current.person_id == person.id else skipped).append(pid)
                continue
            link_in_session(session, player["lineup_id"], pid, person, None, body.author, body.reason)
            named.append(pid)
        return {
            "person_id": person.id,
            "named": named,
            "already_named": unchanged,
            "kept_other_names": skipped,
        }


def group_faces(db, body: GroupFaces):
    with db.snapshot_session(write=True) as session:
        index = face_index(session, db.settings.data_dir)
        if body.expected_fingerprint != index["fingerprint"]:
            raise Conflict("Faces changed; reload before grouping")
        selected = set(body.player_ids)
        if len(selected) < 2 or not selected <= index["players"].keys():
            raise ValueError("Choose at least two current face groups")
        roots = index["confirmed_roots"]
        selected_roots = {roots[pid] for pid in selected}
        members = {pid for pid, root in roots.items() if root in selected_roots}
        names = {index["named"][pid]["person_id"] for pid in members if pid in index["named"]}
        if len(names) > 1:
            raise Conflict("These faces have different names; remove the incorrect name before grouping")
        for separation in index["separations"]:
            other = separation["other"]
            if separation["player_id"] in members and (
                other.get("player_id") in members or other.get("person_id") in names
            ):
                raise Conflict("These faces were kept apart; undo that separation before grouping")
        document = {"player_ids": sorted(selected), "basis": "operator_grouped"}
        row = LineupGrouping(sha256=digest(document), document=document)
        session.add(row)
        session.flush()
        save_revision(session, "grouping", row, body.author, body.reason, None)
        return checked(row)


def remove_grouping(db, grouping_id, review: Review):
    with db.snapshot_session(write=True) as session:
        row = session.get(LineupGrouping, grouping_id)
        if row is None:
            raise ValueError("Grouping not found")
        before = checked(row)
        session.delete(row)
        session.flush()
        save_revision(session, "grouping", None, review.author, review.reason, before)
        return {"id": grouping_id, "removed": True}


def separate(db, player_id, body: Separate):
    with db.snapshot_session(write=True) as session:
        index = face_index(session, db.settings.data_dir)
        if player_id not in index["players"]:
            raise ValueError("Lineup player not found")
        if body.from_player_id is not None:
            if body.from_player_id not in index["players"] or body.from_player_id == player_id:
                raise ValueError("Choose another game player to separate from")
            roots = index["confirmed_roots"]
            if roots[player_id] == roots[body.from_player_id]:
                raise Conflict(
                    "These faces are confirmed as one person; undo their grouping or name links first"
                )
            other = {"player_id": body.from_player_id}
        else:
            if session.get(LineupPerson, body.from_person_id) is None:
                raise ValueError("Person not found")
            roots = index["confirmed_roots"]
            if any(
                roots[pid] == roots[player_id] and link["person_id"] == body.from_person_id
                for pid, link in index["named"].items()
            ):
                raise Conflict("This player carries that name; remove the name instead")
            other = {"person_id": body.from_person_id}
        document = {"player_id": player_id, "other": other, "basis": "operator_separated"}
        row = LineupSeparation(player_id=player_id, sha256=digest(document), document=document)
        session.add(row)
        session.flush()
        save_revision(session, "separation", row, body.author, body.reason, None)
        return checked(row)


def remove_separation(db, separation_id, review: Review):
    with db.snapshot_session(write=True) as session:
        row = session.get(LineupSeparation, separation_id)
        if row is None:
            raise ValueError("Separation not found")
        before = checked(row)
        session.delete(row)
        session.flush()
        save_revision(session, "separation", None, review.author, review.reason, before)
        return {"id": separation_id, "removed": True}


def player_lineup_id(session, data_dir, player_id):
    """The lineup that holds a player, from the cached index when it is current."""
    index = face_index(session, data_dir)
    player = index["players"].get(player_id)
    return player["lineup_id"] if player else None


def lineup_overview(session, data_dir):
    """Games, people and team games without sending every game's players."""
    index = face_index(session, data_dir)
    lineups = sorted(
        index["lineups"].values(), key=lambda x: (x["title_date"] or "", x["sheet"] or 0, x["start_seconds"])
    )
    links_by_lineup = {}
    for link in index["links"].values():
        links_by_lineup.setdefault(link["lineup_id"], []).append(link)
    persons = index["persons"]
    people = []
    for person in persons.values():
        lineup_ids = sorted(
            {x["lineup_id"] for x in index["links"].values() if x["person_id"] == person["id"]}
        )
        people.append({**person, "lineup_ids": lineup_ids})
    people.sort(key=lambda p: ((p["team"] or "~").lower(), p["name"].lower()))
    games = []
    for lineup in lineups:
        links = links_by_lineup.get(lineup["id"], [])
        games.append(
            {
                **lineup_summary(lineup),
                "face_count": lineup["face_count"],
                "player_count": sum(p["role"] == "player" for p in lineup["players"].values()),
                "named_people": len(links),
                "names": sorted({persons[x["person_id"]]["name"] for x in links}),
            }
        )
    teams = []
    for team in sorted({p["team"] for p in persons.values() if p["team"]}):
        rows = []
        for lineup in lineups:
            present = sorted(
                {
                    persons[x["person_id"]]["name"]
                    for x in links_by_lineup.get(lineup["id"], [])
                    if persons[x["person_id"]]["team"] == team
                }
            )
            if present:
                rows.append(
                    {
                        "lineup_id": lineup["id"],
                        "source_id": lineup["source_id"],
                        "title": lineup["title"],
                        "title_date": lineup["title_date"],
                        "sheet": lineup["sheet"],
                        "start_seconds": lineup["start_seconds"],
                        "members_present": present,
                        "confidence": "team_game" if len(present) >= 2 else "one_member_only",
                    }
                )
        teams.append(
            {
                "team": team,
                "people": sorted(p["name"] for p in persons.values() if p["team"] == team),
                "games": rows,
            }
        )
    return {"lineups": games, "people": people, "teams": teams}
