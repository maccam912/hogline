"""Local lineup gallery routes. Biometric templates never leave the server process."""

import io
from collections import OrderedDict

import numpy as np
from fastapi.responses import Response
from PIL import Image
from pydantic import Field
from sqlalchemy import select

from hogline.analytics.biometrics import delete_archive, read_archive
from hogline.db import AutoLineup, GameLineup, Job, LineupLink, LineupSegment, ProcessingBatch
from hogline.domain.catalog import Conflict
from hogline.domain.identities import (
    GroupFaces,
    NameIdentity,
    Separate,
    face_detail,
    face_index,
    faces_overview,
    group_faces,
    lineup_overview,
    name_identity,
    names_by_source,
    player_lineup_id,
    remove_grouping,
    remove_separation,
    separate,
    source_faces,
)
from hogline.domain.lineups import (
    PersonEdit,
    PlayerLink,
    Review,
    checked,
    delete_lineup,
    delete_lineup_in_session,
    delete_person,
    edit_person,
    lineup_detail,
    lineups_in_session,
    link_player,
    more_like_person,
    unlink_player,
)
from hogline.domain.models import Model
from hogline.orchestration import auto_lineups
from hogline.orchestration.lineups import (
    ASSEMBLY_KIND,
    KIND,
    LineupRequest,
    create_lineup_batch,
    enqueue_assembly,
)
from hogline.orchestration.plans import batch_view
from hogline.perception.faces import face_model_status
from hogline.reconstruction.lineups import DEFAULTS


class RevisionReview(Review):
    expected_revision: int = Field(ge=1)


class Regroup(Review):
    distance_threshold: float = Field(default=DEFAULTS["distance_threshold"], ge=0.2, le=0.9)
    clothing_weight: float = Field(default=DEFAULTS["clothing_weight"], ge=0, le=1)
    min_player_faces: int = Field(default=DEFAULTS["min_player_faces"], ge=1, le=500)


class Backfill(Model):
    source_ids: list[str] | None = Field(default=None, max_length=5000)


class ThumbnailCache:
    def __init__(self, size=48):
        self.size, self.items = size, OrderedDict()

    def get(self, data_dir, segment):
        key = (segment["archive"]["path"], segment["archive"]["sha256"])
        if key not in self.items:
            self.items[key] = read_archive(data_dir, segment["archive"])["thumbnails"]
            while len(self.items) > self.size:
                self.items.popitem(last=False)
        self.items.move_to_end(key)
        return self.items[key]


def register_lineups(app, db):
    thumbnails = ThumbnailCache()

    def progress(session):
        batches = [
            batch_view(session, b)
            for b in session.scalars(select(ProcessingBatch))
            if b.document.get("kind") == KIND and b.state in ("active", "paused")
        ]
        assemblies = [
            {
                "id": j.id,
                "state": j.state,
                "error": j.document.get("error"),
                **{k: j.document.get(k) for k in ("lineup_batch_id", "finished_at")},
            }
            for j in session.scalars(select(Job).where(Job.state.in_(["queued", "running", "failed"])))
            if j.document.get("kind") == ASSEMBLY_KIND
        ]
        return batches, assemblies

    @app.get("/api/lineups")
    def gallery(view: str = "full"):
        """`view=summary` lists games without their players; open one with /api/lineups/{id}."""
        if view == "summary":
            with db.snapshot_session() as session:
                summary = lineup_overview(session, db.settings.data_dir)
                batches, assemblies = progress(session)
            return {
                **summary,
                "positions": ["lead", "second", "third", "skip", "alternate"],
                "batches": batches,
                "assemblies": assemblies,
                "face_models": face_model_status(db.settings.data_dir),
                "automatic": auto_lineups.status(db),
            }
        with db.snapshot_session() as session:
            view = lineups_in_session(session, db.settings.data_dir)
            batches = [
                batch_view(session, b)
                for b in session.scalars(select(ProcessingBatch))
                if b.document.get("kind") == KIND and b.state in ("active", "paused")
            ]
            assemblies = [
                {
                    "id": j.id,
                    "state": j.state,
                    "error": j.document.get("error"),
                    **{k: j.document.get(k) for k in ("lineup_batch_id", "finished_at")},
                }
                for j in session.scalars(select(Job).where(Job.state.in_(["queued", "running", "failed"])))
                if j.document.get("kind") == ASSEMBLY_KIND
            ]
        models = face_model_status(db.settings.data_dir)
        return {**view, "batches": batches, "assemblies": assemblies, "face_models": models}

    @app.get("/api/lineups/history.json")
    def history():
        with db.snapshot_session() as session:
            return lineups_in_session(session, db.settings.data_dir, history=True)["history"]

    @app.post("/api/lineups")
    def create(request: LineupRequest):
        return create_lineup_batch(db, request)

    @app.get("/api/lineups/automatic")
    def automatic_status():
        return auto_lineups.status(db)

    @app.post("/api/lineups/automatic/backfill")
    def automatic_backfill(body: Backfill):
        """Queue analyzed recordings without lineups: all of them, or the IDs given."""
        return auto_lineups.backfill(db, body.source_ids)

    @app.post("/api/lineups/automatic/{source_id}/{action}")
    def automatic_control(source_id: str, action: str):
        if action not in ("queue", "skip"):
            raise ValueError("Use queue or skip")
        return auto_lineups.control(db, source_id, action)

    @app.get("/api/lineups/{lineup_id}")
    def one_lineup(lineup_id: str):
        with db.snapshot_session() as session:
            result = lineup_detail(session, db.settings.data_dir, lineup_id)
            index = face_index(session, db.settings.data_dir)
            pids = {p["id"] for p in result["lineup"]["players"]}
            result["fingerprint"] = index["fingerprint"]
            result["groupings"] = [g for g in index["groupings"] if pids.intersection(g["player_ids"])]
            for player in result["lineup"]["players"]:
                player["group_id"] = index["confirmed_roots"][player["id"]]
            return result

    @app.post("/api/faces/groupings")
    def group_selected_faces(body: GroupFaces):
        return group_faces(db, body)

    @app.post("/api/faces/groupings/{grouping_id}/delete")
    def undo_face_grouping(grouping_id: str, body: Review):
        return remove_grouping(db, grouping_id, body)

    @app.get("/api/faces")
    def faces_gallery(
        q: str = "", named: bool | None = None, min_games: int = 1, offset: int = 0, limit: int = 200
    ):
        """Cross-game identities: one representative face per probable person."""
        with db.snapshot_session() as session:
            return faces_overview(
                session,
                db.settings.data_dir,
                query=q[:160],
                named=named,
                min_games=max(1, min_games),
                offset=max(0, offset),
                limit=max(1, min(limit, 1000)),
            )

    @app.get("/api/faces/names")
    def face_names():
        """Names and teams seen in each recording, for archive search."""
        with db.snapshot_session() as session:
            return names_by_source(session, db.settings.data_dir)

    @app.get("/api/faces/sources/{source_id}")
    def faces_in_source(source_id: str):
        with db.snapshot_session() as session:
            row = session.get(AutoLineup, source_id)
            return {
                "games": source_faces(session, db.settings.data_dir, source_id),
                "automatic": None
                if row is None
                else {
                    "state": row.state,
                    "problem": row.document.get("problem"),
                    "skip_reason": row.document.get("skip_reason"),
                },
            }

    @app.get("/api/faces/players/{player_id}")
    def face(player_id: str):
        """Every game this face's probable person appears in, plus possible matches."""
        with db.snapshot_session() as session:
            return face_detail(session, db.settings.data_dir, player_id)

    @app.put("/api/faces/players/{player_id}/name")
    def name_face(player_id: str, body: NameIdentity):
        return name_identity(db, player_id, body)

    @app.post("/api/faces/players/{player_id}/separate")
    def not_this_person(player_id: str, body: Separate):
        return separate(db, player_id, body)

    @app.post("/api/faces/separations/{separation_id}/delete")
    def undo_separation(separation_id: str, body: Review):
        return remove_separation(db, separation_id, body)

    @app.post("/api/lineups/{lineup_id}/regroup")
    def regroup(lineup_id: str, body: Regroup):
        with db.snapshot_session(write=True) as session:
            row = session.get(GameLineup, lineup_id)
            if row is None:
                raise ValueError("Lineup not found")
            if session.scalar(select(LineupLink).where(LineupLink.lineup_id == lineup_id)) is not None:
                raise Conflict("Remove this game's names before regrouping its players")
            index = face_index(session, db.settings.data_dir)
            pids = {p["id"] for p in row.document["players"]}
            if any(pids.intersection(g["player_ids"]) for g in index["groupings"]):
                raise Conflict("Undo this game's manual face groupings before automatic regrouping")
            batch = session.get(ProcessingBatch, checked(row)["batch_id"])
            params = body.model_dump(include={"distance_threshold", "clothing_weight", "min_player_faces"})
            job = enqueue_assembly(session, batch, params)
            return {"job_id": job.id, "state": job.state}

    @app.post("/api/lineups/{lineup_id}/rescan")
    def rescan(lineup_id: str, body: Review):
        from uuid import uuid4

        from hogline.orchestration.lineups import lineup_batch_in_session, lineup_models

        # Resolve models before touching existing faces; all DB changes commit together.
        with db.snapshot_session() as session:
            row = session.get(GameLineup, lineup_id)
            if row is None:
                raise ValueError("Lineup not found; reload this recording")
            original = checked(row)
            original_sha = row.sha256
            request = LineupRequest.model_validate(
                {
                    key: value
                    for key, value in original["request"].items()
                    if key in LineupRequest.model_fields
                }
            )
        embedder, version = lineup_models(db.settings.data_dir, request.embedder)
        with db.snapshot_session(write=True) as session:
            row = session.get(GameLineup, lineup_id)
            if row is None or row.sha256 != original_sha:
                raise Conflict("Lineup changed; reload before rescanning")
            for batch in session.scalars(
                select(ProcessingBatch).where(ProcessingBatch.state.in_(["active", "paused"]))
            ):
                if request.source_id in batch.document.get("request", {}).get("source_ids", []):
                    raise ValueError("Cancel or finish this recording’s active jobs before rescanning")
            for job in session.scalars(select(Job).where(Job.state.in_(["running", "queued"]))):
                if job.document.get("request", {}).get("source_id") == request.source_id:
                    raise ValueError("Wait for this recording’s jobs to stop before rescanning")
            removed = delete_lineup_in_session(session, lineup_id, body)
            result = lineup_batch_in_session(session, request, embedder, version, rerun_nonce=str(uuid4()))
        for archive in removed:
            delete_archive(db.settings.data_dir, archive)
        return result

    @app.post("/api/lineups/{lineup_id}/delete")
    def remove(lineup_id: str, body: Review):
        return delete_lineup(db, lineup_id, body)

    @app.get("/api/lineups/{lineup_id}/players/{player_id}/faces.jpg")
    def faces(lineup_id: str, player_id: str, limit: int = 12, size: int = 72):
        limit, size = max(1, min(limit, 24)), max(32, min(size, 128))
        with db.snapshot_session() as session:
            row = session.get(GameLineup, lineup_id)
            if row is None:
                raise ValueError("Lineup not found")
            lineup = checked(row)
            player = next((p for p in lineup["players"] if p["id"] == player_id), None)
            if player is None:
                raise ValueError("Lineup player not found")
            segments = {}
            for ref in {t["segment_id"] for t in player["thumbnails"]}:
                segment = session.get(LineupSegment, ref)
                if segment is None:
                    raise ValueError("Lineup segment was deleted")
                segments[ref] = checked(segment)
        tiles = [
            Image.fromarray(
                thumbnails.get(db.settings.data_dir, segments[t["segment_id"]])[t["thumb"]]
            ).resize((size, size), Image.Resampling.LANCZOS)
            for t in player["thumbnails"][:limit]
        ]
        if not tiles:
            tiles = [Image.fromarray(np.full((size, size, 3), 40, dtype=np.uint8))]
        strip = Image.new("RGB", (size * len(tiles), size))
        for n, tile in enumerate(tiles):
            strip.paste(tile, (n * size, 0))
        buffer = io.BytesIO()
        strip.save(buffer, "JPEG", quality=88)
        return Response(
            buffer.getvalue(), media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"}
        )

    portraits = OrderedDict()

    @app.get("/api/lineups/{lineup_id}/players/{player_id}/portrait.jpg")
    def portrait(lineup_id: str, player_id: str, size: int = 96):
        """The player's representative face (or its largest face for older lineups)."""
        size = max(32, min(size, 256))
        with db.snapshot_session() as session:
            player = face_index(session, db.settings.data_dir)["players"].get(player_id)
            if player is not None and player["lineup_id"] == lineup_id:
                face = player["portrait"]
            else:
                row = session.get(GameLineup, lineup_id)
                if row is None:
                    raise ValueError("Lineup not found")
                full = next((p for p in checked(row)["players"] if p["id"] == player_id), None)
                if full is None:
                    raise ValueError("Lineup player not found")
                face = full.get("portrait") or (full["thumbnails"][0] if full["thumbnails"] else None)
            key = (face["segment_id"], face["thumb"], size) if face else None
            segment = None
            if key is not None and key not in portraits:
                row = session.get(LineupSegment, face["segment_id"])
                if row is None:
                    raise ValueError("Lineup segment was deleted")
                segment = checked(row)
        if key is None:
            image = Image.fromarray(np.full((size, size, 3), 40, dtype=np.uint8))
            buffer = io.BytesIO()
            image.save(buffer, "JPEG", quality=90)
            content = buffer.getvalue()
        elif key in portraits:
            portraits.move_to_end(key)
            content = portraits[key]
        else:
            tile = thumbnails.get(db.settings.data_dir, segment)[face["thumb"]]
            buffer = io.BytesIO()
            Image.fromarray(tile).resize((size, size), Image.Resampling.LANCZOS).save(
                buffer, "JPEG", quality=90
            )
            content = portraits[key] = buffer.getvalue()
            while len(portraits) > 5000:
                portraits.popitem(last=False)
        return Response(content, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})

    @app.put("/api/lineups/players/{player_id}/link")
    def link(player_id: str, body: PlayerLink):
        with db.snapshot_session() as session:
            lineup_id = player_lineup_id(session, db.settings.data_dir, player_id)
        return link_player(db, player_id, body, lineup_id)

    @app.post("/api/lineups/players/{player_id}/unlink")
    def unlink(player_id: str, body: RevisionReview):
        return unlink_player(db, player_id, body.expected_revision, body)

    @app.put("/api/lineups/people/{person_id}")
    def person(person_id: str, body: PersonEdit):
        return edit_person(db, person_id, body)

    @app.post("/api/lineups/people/{person_id}/delete")
    def forget(person_id: str, body: RevisionReview):
        return delete_person(db, person_id, body.expected_revision, body)

    @app.get("/api/lineups/people/{person_id}/more")
    def more(person_id: str):
        with db.snapshot_session() as session:
            return more_like_person(session, db.settings.data_dir, person_id)
