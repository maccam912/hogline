import hashlib
import io
import math

from fastapi import HTTPException
from fastapi.responses import Response
from PIL import Image

from hogline.db import ActorIdentity, Evidence
from hogline.domain.players import (
    GroupEdit,
    IdentityMove,
    actor_result,
    checked,
    edit_group,
    move_fragment,
    players_in_session,
    sync_players,
)


def register_players(app, db):
    @app.get("/api/players")
    def players():
        with db.snapshot_session() as session:
            return players_in_session(session)

    @app.post("/api/players/sync")
    def sync(after_id: str = "", limit: int = 50):
        return sync_players(db, after_id, limit)

    @app.get("/api/players/history.json")
    def history():
        with db.snapshot_session() as session:
            return players_in_session(session, history=True)

    @app.put("/api/players/groups/{identifier}")
    def rename(identifier: str, body: GroupEdit):
        return edit_group(db, identifier, body)

    @app.put("/api/players/fragments/{identifier}")
    def move(identifier: str, body: IdentityMove):
        return move_fragment(db, identifier, body)

    @app.get("/api/players/fragments/{identifier}/thumbnail")
    def thumbnail(identifier: str):
        with db.snapshot_session() as session:
            assignment = session.get(ActorIdentity, identifier)
            if assignment is None:
                raise HTTPException(404, "Player fragment not found")
            info = checked(assignment)
            result = actor_result(session, assignment.actor_result_id)
            if info["actor_result_sha256"] != result["sha256"]:
                raise ValueError("Player thumbnail parent checksum mismatch")
            track = next((t for t in result["tracks"] if t["id"] == assignment.track_id), None)
            if track is None:
                raise ValueError("Player thumbnail track not found")
            for evidence_id in result["context_anchor_evidence_ids"]:
                row = session.get(Evidence, evidence_id)
                if row is None or row.source_id != result["source_id"]:
                    raise ValueError("Player thumbnail context/source mismatch")
                frame = row.document
                point = next(
                    (
                        p
                        for p in track["points"]
                        if all(p[k] == frame[k] for k in ("pts", "time_base_num", "time_base_den"))
                    ),
                    None,
                )
                if point is None:
                    continue
                asset = frame["crops"][track["view"]]
                if (asset["width"], asset["height"]) != (track["crop_width"], track["crop_height"]):
                    continue
                path = (db.settings.data_dir / asset["path"]).resolve()
                if (
                    not path.is_relative_to((db.settings.data_dir / "evidence").resolve())
                    or not path.is_file()
                    or path.stat().st_size > 16 * 1024**2
                    or hashlib.sha256(path.read_bytes()).hexdigest() != asset["sha256"]
                ):
                    raise ValueError("Player thumbnail evidence missing or corrupt")
                with Image.open(path) as image:
                    if image.size != (asset["width"], asset["height"]):
                        raise ValueError("Player thumbnail image dimensions disagree")
                    x, y, w, h = point["bbox_px"]
                    bounds = (
                        max(0, math.floor(x)),
                        max(0, math.floor(y)),
                        min(image.width, math.ceil(x + w)),
                        min(image.height, math.ceil(y + h)),
                    )
                    if bounds[2] <= bounds[0] or bounds[3] <= bounds[1]:
                        raise ValueError("Player thumbnail body lies outside the original image")
                    body = image.convert("RGB").crop(bounds)
                    body.thumbnail((180, 240))
                    buffer = io.BytesIO()
                    body.save(buffer, "JPEG", quality=85)
                return Response(
                    buffer.getvalue(), media_type="image/jpeg", headers={"Cache-Control": "no-store"}
                )
        raise HTTPException(404, "No retained original frame exactly matches this body fragment")
