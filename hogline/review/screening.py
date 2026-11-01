"""Small manually labeled recording previews; no identity matching or automatic analysis."""

import hashlib
import json
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import Field
from sqlalchemy import select

from hogline.db import Source, now
from hogline.domain.catalog import Conflict
from hogline.domain.models import Model


class PreviewLabel(Model):
    decision: Literal["team_visible", "not_visible", "unsure"]
    team: str = Field(default="Koski Team", min_length=1, max_length=120)
    note: str = Field(default="", max_length=1000)
    expected_revision: int = Field(ge=0)
    expected_preview_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def previews(root):
    path = root / "screening" / "manifest.json"
    return json.loads(path.read_text())["previews"] if path.is_file() else []


def save_preview_label(db, source_id, body):
    item = next((p for p in previews(db.settings.data_dir) if p["source_id"] == source_id), None)
    if item is None:
        raise ValueError("Preview not found")
    if item["sha256"] != body.expected_preview_sha256:
        raise Conflict("The preview changed; refresh before labeling")
    with db.snapshot_session(write=True) as session:
        source = session.get(Source, source_id)
        if source is None or source.document["metadata_sha256"] != item["metadata_sha256"]:
            raise Conflict("Recording metadata changed; refresh the preview")
        old = source.document.get("manual_preview_label", {})
        if old.get("revision", 0) != body.expected_revision:
            raise Conflict("This preview label changed; refresh before saving")
        label = {
            "revision": body.expected_revision + 1,
            "decision": body.decision,
            "team": body.team.strip(),
            "note": body.note,
            "source_seconds": item["source_seconds"],
            "preview_sha256": item["sha256"],
            "author": "local operator",
            "at": now(),
            "scope": "this_preview_only",
        }
        if not label["team"]:
            raise ValueError("Enter a team name")
        source.document = {
            **source.document,
            "manual_preview_label": label,
            "preview_label_history": [*source.document.get("preview_label_history", []), label],
        }
        return label


def router(db):
    routes = APIRouter()

    @routes.get("/screening")
    def page():
        return FileResponse(Path(__file__).parent / "static" / "screening.html")

    @routes.get("/api/screening")
    def catalog():
        items = previews(db.settings.data_dir)
        with db.snapshot_session() as session:
            sources = {
                r.id: r.document
                for r in session.scalars(select(Source).where(Source.id.in_([p["source_id"] for p in items])))
            }
        manifest_path = db.settings.data_dir / "screening" / "manifest.json"
        manifest = json.loads(manifest_path.read_text()) if manifest_path.is_file() else {}
        return {
            "season": manifest.get("season"),
            "notice": manifest.get("notice"),
            "previews": [
                {
                    **p,
                    "label": (
                        sources[p["source_id"]].get("manual_preview_label")
                        if sources[p["source_id"]].get("manual_preview_label", {}).get("preview_sha256")
                        == p["sha256"]
                        else None
                    ),
                    "label_revision": sources[p["source_id"]]
                    .get("manual_preview_label", {})
                    .get("revision", 0),
                    "stale": sources[p["source_id"]]["metadata_sha256"] != p["metadata_sha256"],
                }
                for p in items
                if p["source_id"] in sources
            ],
        }

    @routes.get("/api/screening/{source_id}/preview.jpg")
    def preview(source_id: str, sha: str | None = None):
        item = next((p for p in previews(db.settings.data_dir) if p["source_id"] == source_id), None)
        if item is None:
            raise HTTPException(404, "Preview not found")
        if sha is not None:
            item = next((p for p in [item, *item.get("additional_previews", [])] if p["sha256"] == sha), None)
            if item is None:
                raise HTTPException(404, "Preview not found")
        path = db.settings.data_dir / "screening" / f"{item['sha256']}.jpg"
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
            raise HTTPException(410, "Preview missing or changed")
        return FileResponse(path, media_type="image/jpeg")

    @routes.put("/api/screening/{source_id}/label")
    def label(source_id: str, body: PreviewLabel):
        return save_preview_label(db, source_id, body)

    return routes
