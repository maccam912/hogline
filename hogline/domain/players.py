"""Persistent anonymous groups and reversible, audited non-biometric fragment links."""

from fractions import Fraction
from uuid import NAMESPACE_URL, uuid4, uuid5

from pydantic import Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.orm.exc import StaleDataError

from hogline.adapters.sources import digest
from hogline.db import ActorIdentity, Evidence, PlayerGroup, PlayerRevision, WindowActors
from hogline.domain.catalog import Conflict
from hogline.domain.models import Model

VERSION = "anonymous-player-groups-v1"


class Review(Model):
    author: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def nonblank(self):
        if not self.author.strip() or not self.reason.strip():
            raise ValueError("Reviewer and reason cannot be blank")
        return self


class GroupEdit(Review):
    expected_revision: int = Field(ge=1)
    name: str | None = Field(default=None, max_length=160)
    note: str = Field(default="", max_length=2000)


class IdentityMove(Review):
    expected_revision: int = Field(ge=1)
    expected_source_revision: int = Field(ge=1)
    target_group_id: str | None = None
    expected_target_revision: int | None = Field(default=None, ge=1)


def checked(row):
    if digest(row.document) != row.sha256:
        raise ValueError("Player identity record checksum mismatch")
    if isinstance(row, ActorIdentity) and any(
        row.document.get(k) != getattr(row, k)
        for k in (
            "actor_result_id",
            "track_id",
            "group_id",
            "source_id",
            "view",
            "start_seconds",
            "end_seconds",
        )
    ):
        raise ValueError("Player identity references disagree with their checksum")
    return {"id": row.id, "revision": row.revision, "sha256": row.sha256, **row.document}


def save_revision(session, row, kind, author, reason, before=None):
    document = {
        "after": checked(row),
        "before": before,
    }
    session.add(
        PlayerRevision(
            kind=kind,
            entity_id=row.id,
            number=row.revision,
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
        raise Conflict("Player grouping changed in another review; reload before saving") from exc


def new_group(session, identifier=None):
    document = {"name": None, "note": ""}
    row = PlayerGroup(id=identifier or str(uuid4()), sha256=digest(document), document=document)
    session.add(row)
    session.flush()
    save_revision(session, row, "group", "Hogline", "Create anonymous body-fragment group")
    return row


def same_original_track(a, b):
    """Duplicate overlap only: no appearance embedding, face, or gap extrapolation."""
    if any(a.get(k) != b.get(k) for k in ("view", "crop_width", "crop_height")):
        return False

    def key(p):
        return Fraction(p["pts"] * p["time_base_num"], p["time_base_den"])

    left = {key(p): p for p in a["points"]}
    common = [(left[key(p)], p) for p in b["points"] if key(p) in left]
    if len(common) < 3 or max(key(x) for x, _ in common) - min(key(x) for x, _ in common) < 1:
        return False
    return all(
        x.get("decoded_rgb_sha256")
        and x.get("decoded_rgb_sha256") == y.get("decoded_rgb_sha256")
        and all(abs(v - w) < 0.00001 for v, w in zip(x["bbox_px"], y["bbox_px"], strict=True))
        for x, y in common
    )


def actor_result(session, identifier):
    from hogline.analytics.actors import checked_actor_result

    row = session.get(WindowActors, identifier)
    if row is None:
        raise ValueError("Player fragment references a missing actor result")
    return checked_actor_result(row)


def register_fragments(session, result):
    # A bounded window adds only new immutable fragments. Existing manual decisions win.
    results = {result["id"]: result}
    for track in result["tracks"]:
        identifier = str(uuid5(NAMESPACE_URL, f"player-fragment:{result['id']}:{track['id']}"))
        if session.get(ActorIdentity, identifier):
            continue
        matches = []
        previous = list(
            session.scalars(
                select(ActorIdentity)
                .where(
                    ActorIdentity.source_id == result["source_id"],
                    ActorIdentity.view == track["view"],
                    ActorIdentity.start_seconds <= track["end_seconds"],
                    ActorIdentity.end_seconds >= track["start_seconds"],
                )
                .limit(129)
            )
        )
        candidate_limit = len(previous) > 128
        for assignment in [] if candidate_limit else previous:
            info = checked(assignment)
            if info["source_id"] != result["source_id"] or info["view"] != track["view"]:
                continue
            if info["end_seconds"] < track["start_seconds"] or info["start_seconds"] > track["end_seconds"]:
                continue
            parent = results.setdefault(assignment.actor_result_id, None)
            if parent is None:
                parent = results[assignment.actor_result_id] = actor_result(
                    session, assignment.actor_result_id
                )
            if any(
                parent[k] != result[k]
                for k in ("source_metadata_sha256", "profile_sha256", "detector_version")
            ):
                continue
            other = next((t for t in parent["tracks"] if t["id"] == assignment.track_id), None)
            if other is not None and same_original_track(track, other):
                matches.append(assignment)
        groups = {m.group_id for m in matches}
        if len(groups) == 1:
            group = session.get(PlayerGroup, next(iter(groups)))
            checked(group)
            before = checked(group)
            touch(session, group, dict(group.document))
            save_revision(session, group, "group", "Hogline", "Add identical original-frame overlap", before)
            basis = "identical_original_frame_overlap"
        else:
            group = new_group(session, str(uuid5(NAMESPACE_URL, f"anonymous-player:{identifier}")))
            basis = (
                "overlap_candidate_limit_kept_separate"
                if candidate_limit
                else "anonymous_body_fragment"
                if not groups
                else "ambiguous_overlap_kept_separate"
            )
        document = {
            "actor_result_id": result["id"],
            "track_id": track["id"],
            "group_id": group.id,
            "source_id": result["source_id"],
            "actor_result_sha256": result["sha256"],
            "view": track["view"],
            "start_seconds": track["start_seconds"],
            "end_seconds": track["end_seconds"],
            "basis": basis,
            "overlap_assignment_ids": sorted(m.id for m in matches),
            "acceptance": "proposed",
            "face_recognition": False,
        }
        row = ActorIdentity(
            id=identifier,
            actor_result_id=result["id"],
            track_id=track["id"],
            group_id=group.id,
            source_id=result["source_id"],
            view=track["view"],
            start_seconds=track["start_seconds"],
            end_seconds=track["end_seconds"],
            sha256=digest(document),
            document=document,
        )
        session.add(row)
        session.flush()
        save_revision(session, row, "assignment", "Hogline", basis)


def sync_players(db, after_id="", limit=50):
    if not 1 <= limit <= 100:
        raise ValueError("Sync one to 100 actor results at a time")
    with db.snapshot_session(write=True) as session:
        rows = list(
            session.scalars(
                select(WindowActors)
                .where(WindowActors.id > after_id)
                .order_by(WindowActors.id)
                .limit(limit + 1)
            )
        )
        for row in rows[:limit]:
            register_fragments(session, actor_result(session, row.id))
        return {
            "processed": min(limit, len(rows)),
            "next_cursor": rows[limit - 1].id if len(rows) > limit else None,
        }


def players_in_session(session, *, history=False):
    groups = [
        {**checked(g), "members": []} for g in session.scalars(select(PlayerGroup).order_by(PlayerGroup.id))
    ]
    by_id = {g["id"]: g for g in groups}
    parents, context_frames = {}, {}
    for row in session.scalars(select(ActorIdentity).order_by(ActorIdentity.id)):
        info = checked(row)
        if row.actor_result_id not in parents:
            parents[row.actor_result_id] = actor_result(session, row.actor_result_id)
        parent = parents[row.actor_result_id]
        track = next((t for t in parent["tracks"] if t["id"] == row.track_id), None)
        if (
            info["actor_result_sha256"] != parent["sha256"]
            or track is None
            or info["source_id"] != parent["source_id"]
            or any(info[k] != track[k] for k in ("view", "start_seconds", "end_seconds"))
        ):
            raise ValueError("Player membership disagrees with its immutable actor evidence")
        if row.group_id not in by_id:
            raise ValueError("Player fragment group is missing")
        preview = None
        for evidence_id in parent["context_anchor_evidence_ids"]:
            if evidence_id not in context_frames:
                context_frames[evidence_id] = session.get(Evidence, evidence_id)
            frame = context_frames[evidence_id]
            if frame is None or frame.source_id != parent["source_id"]:
                raise ValueError("Player preview context/source mismatch")
            asset = frame.document["crops"].get(track["view"])
            if (
                asset
                and asset["width"] == track["crop_width"]
                and asset["height"] == track["crop_height"]
                and any(
                    all(p[k] == frame.document[k] for k in ("pts", "time_base_num", "time_base_den"))
                    for p in track["points"]
                )
            ):
                preview = evidence_id
                break
        by_id[row.group_id]["members"].append(
            {
                **info,
                "actor_result_id": row.actor_result_id,
                "track_id": row.track_id,
                "window_id": parent["window_id"],
                "preview_evidence_id": preview,
                "group_id": row.group_id,
            }
        )
    output = {
        "version": VERSION,
        "groups": groups,
        "notice": (
            "Persistent anonymous body groups, optionally named by you. One person may have multiple groups. "
            "Automatic links require identical bodies in overlapping original frames; no face recognition or "
            "automatic cross-recording identity. Grouping does not confirm a thrower "
            "or enable weight statistics."
        ),
    }
    if history:
        revisions = []
        for row in session.scalars(
            select(PlayerRevision).order_by(PlayerRevision.created_at, PlayerRevision.id)
        ):
            if digest(row.document) != row.sha256:
                raise ValueError("Player revision checksum mismatch")
            revisions.append(
                {
                    "id": row.id,
                    "kind": row.kind,
                    "entity_id": row.entity_id,
                    "revision": row.number,
                    "author": row.author,
                    "reason": row.reason,
                    "sha256": row.sha256,
                    **row.document,
                }
            )
        output["history"] = revisions
    return output


def edit_group(db, identifier, edit: GroupEdit):
    with db.snapshot_session(write=True) as session:
        row = session.get(PlayerGroup, identifier)
        if row is None:
            raise ValueError("Player group not found")
        before = checked(row)
        if row.revision != edit.expected_revision:
            raise Conflict("Player group changed; reload before saving")
        touch(
            session,
            row,
            {"name": edit.name.strip() or None if edit.name is not None else None, "note": edit.note},
        )
        save_revision(session, row, "group", edit.author, edit.reason, before)
        return checked(row)


def move_fragment(db, identifier, edit: IdentityMove):
    with db.snapshot_session(write=True) as session:
        row = session.get(ActorIdentity, identifier)
        if row is None:
            raise ValueError("Player fragment not found")
        source = session.get(PlayerGroup, row.group_id)
        before = {**checked(row), "group_id": row.group_id}
        if row.revision != edit.expected_revision or source.revision != edit.expected_source_revision:
            raise Conflict("Player grouping changed; reload before moving a fragment")
        checked(source)
        target = session.get(PlayerGroup, edit.target_group_id) if edit.target_group_id else None
        if edit.target_group_id:
            if target is None:
                raise ValueError("Target player group not found")
            checked(target)
            if target.revision != edit.expected_target_revision:
                raise Conflict("Target player group changed; reload before moving a fragment")
            if target.id == source.id:
                raise ValueError("Fragment already belongs to that player group")
        else:
            if edit.expected_target_revision is not None:
                raise ValueError("New anonymous groups have no existing target revision")
            target = new_group(session)
        for group in (source, target):
            old = checked(group)
            touch(session, group, dict(group.document))
            save_revision(session, group, "group", edit.author, edit.reason, old)
        row.group_id = target.id
        touch(
            session,
            row,
            {**row.document, "group_id": target.id, "basis": "user_link", "acceptance": "human_confirmed"},
        )
        save_revision(session, row, "assignment", edit.author, edit.reason, before)
        return {**checked(row), "group_id": row.group_id}
