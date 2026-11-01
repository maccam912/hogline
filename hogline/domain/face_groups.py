"""Explicit same-person decisions, separate from names and machine face clusters."""

from sqlalchemy import select

from hogline.db import LineupGrouping, LineupLink
from hogline.domain.catalog import Conflict


def components(player_ids, groups):
    """Stable connected components for overlapping sets of player IDs."""
    parent = {pid: pid for pid in player_ids}

    def root(pid):
        while parent[pid] != pid:
            parent[pid] = parent[parent[pid]]
            pid = parent[pid]
        return pid

    for group in groups:
        members = [pid for pid in group if pid in parent]
        if members:
            anchor = min(root(pid) for pid in members)
            for pid in members:
                parent[root(pid)] = anchor
    return {pid: root(pid) for pid in parent}


def confirmed_components(players, groupings, links):
    named = {}
    for pid, link in links.items():
        named.setdefault(link["person_id"], []).append(pid)
    return components(players, [g["player_ids"] for g in groupings] + list(named.values()))


def check_group_name(session, player_id, person_id):
    """A single-player name edit cannot introduce two names into a manual group."""
    groups = [r.document["player_ids"] for r in session.scalars(select(LineupGrouping))]
    members = {pid for group in groups for pid in group} | {player_id}
    roots = components(members, groups)
    for link in session.scalars(select(LineupLink)):
        if (
            link.id != player_id
            and link.id in roots
            and roots[link.id] == roots[player_id]
            and link.person_id != person_id
        ):
            raise Conflict(
                "These faces are grouped with another name; undo the grouping or remove that name first"
            )


def remove_grouped_players(session, player_ids, review):
    from hogline.domain.lineups import checked, save_revision, touch

    removed = set(player_ids)
    for row in session.scalars(select(LineupGrouping)):
        before = checked(row)
        if not removed.intersection(before["player_ids"]):
            continue
        remaining = [pid for pid in before["player_ids"] if pid not in removed]
        if len(remaining) < 2:
            session.delete(row)
            session.flush()
            save_revision(session, "grouping", None, review.author, review.reason, before)
        else:
            touch(session, row, {**row.document, "player_ids": remaining})
            save_revision(session, "grouping", row, review.author, review.reason, before)
