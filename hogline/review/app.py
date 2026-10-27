import hashlib
import shutil
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import Field
from sqlalchemy import select
from starlette.middleware.trustedhost import TrustedHostMiddleware

from hogline import __version__
from hogline.adapters.sources import import_youtube
from hogline.analytics.exports import csv_export, snapshot
from hogline.config import Settings
from hogline.db import Database, Evidence, FrameObservation, Release, Revision, Source
from hogline.domain.catalog import Conflict, catalog, save_game
from hogline.domain.models import GameEdit, Model, ScanRequest
from hogline.orchestration.worker import enqueue, jobs, retry, run_one
from hogline.perception.broadcast import analyze_broadcast, broadcast_catalog
from hogline.perception.pipeline import analyze_cached, observations


class SourceImport(Model):
    url: str = Field(max_length=500)


class ReleaseRequest(Model):
    mode: str = "confirmed"


class BroadcastRequest(Model):
    source_id: str


def create_app(settings: Settings | None = None, start_worker=False):
    db = Database(settings or Settings.load())
    stop = threading.Event()

    def worker_loop():
        while not stop.is_set():
            if run_one(db) is None:
                stop.wait(1)

    @asynccontextmanager
    async def lifespan(_):
        thread = None
        if start_worker:
            thread = threading.Thread(target=worker_loop, daemon=True, name="hogline-worker")
            thread.start()
        yield
        stop.set()
        if thread:
            thread.join(timeout=1)

    app = FastAPI(title="Hogline", version=__version__, lifespan=lifespan)
    app.state.db = db
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])

    @app.middleware("http")
    async def local_origin(request: Request, call_next):
        origin = request.headers.get("origin")
        if request.method not in ("GET", "HEAD", "OPTIONS") and origin:
            if origin != str(request.base_url).rstrip("/"):
                return JSONResponse({"detail": "Mutations require the same local origin"}, status_code=403)
        return await call_next(request)

    @app.exception_handler(ValueError)
    async def invalid(_, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409 if isinstance(exc, Conflict) else 422)

    @app.get("/api/catalog")
    def get_catalog():
        return {
            **catalog(db),
            "jobs": jobs(db),
            "observations": observations(db),
            "broadcast": broadcast_catalog(db),
        }

    @app.post("/api/perception/analyze")
    def analyze():
        return {"observations": analyze_cached(db)}

    @app.post("/api/perception/broadcast")
    def read_broadcast(body: BroadcastRequest):
        with db.session() as session:
            ids = list(session.scalars(select(Evidence.id).where(Evidence.source_id == body.source_id)))
        if not ids:
            raise ValueError("Cache a source frame before reading its clock or board")
        if not shutil.which("tesseract"):
            raise ValueError("Install Tesseract OCR to read retained clock/scoreboard frames locally")
        return {"observations": analyze_broadcast(db, ids)}

    @app.get("/api/broadcast/{observation_id}/{crop}")
    def broadcast_image(observation_id: str, crop: str):
        with db.session() as session:
            row = session.get(FrameObservation, observation_id)
            asset = row.document.get("crops", {}).get(crop) if row else None
            if asset is None:
                raise HTTPException(404, "Broadcast crop not found")
            path = db.settings.data_dir / asset["path"]
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != asset["sha256"]:
                raise HTTPException(410, "Broadcast crop missing or corrupt; use its YouTube timestamp")
            return FileResponse(path, media_type="image/png")

    @app.get("/api/health")
    def health():
        return {"status": "ok", "version": __version__, "worker": start_worker}

    @app.post("/api/sources")
    def add_source(body: SourceImport):
        return import_youtube(db, body.url)

    @app.post("/api/scans")
    def add_scan(body: ScanRequest):
        return enqueue(db, body)

    @app.post("/api/jobs/{job_id}/retry")
    def retry_job(job_id: str):
        return retry(db, job_id)

    @app.put("/api/games/{game_id}")
    def edit_game(game_id: str, body: GameEdit):
        if body.game.id != game_id:
            raise ValueError("Game ID does not match the URL")
        return save_game(db, body)

    @app.get("/api/games/{game_id}/revisions")
    def revisions(game_id: str):
        with db.session() as session:
            return [
                {
                    "revision": r.number,
                    "author": r.author,
                    "reason": r.reason,
                    "created_at": r.created_at,
                    "game": r.document,
                }
                for r in session.scalars(
                    select(Revision).where(Revision.game_id == game_id).order_by(Revision.number.desc())
                )
            ]

    @app.get("/api/evidence/{evidence_id}/{view}")
    def evidence_image(evidence_id: str, view: str):
        with db.session() as session:
            frame = session.get(Evidence, evidence_id)
            if frame is None:
                raise HTTPException(404, "Evidence not found")
            asset = frame.document["image"] if view == "composite" else frame.document["crops"].get(view)
            if asset is None:
                raise HTTPException(404, "View not available")
            path = db.settings.data_dir / asset["path"]
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != asset["sha256"]:
                raise HTTPException(
                    410, "Cached frame is missing or corrupt. Use its original timestamp link."
                )
            return FileResponse(
                path, media_type="image/png", headers={"Cache-Control": "private, max-age=3600"}
            )

    @app.get("/api/media/{source_id}")
    def local_video(source_id: str):
        with db.session() as session:
            source = session.get(Source, source_id)
            if source is None or source.document["platform"] != "local":
                raise HTTPException(404, "Local recording not found")
            path = Path(source.document["local_path"])
            if not path.is_file():
                raise HTTPException(410, "Original recording is missing")
            return FileResponse(path)

    @app.post("/api/releases")
    def release(body: ReleaseRequest):
        result = snapshot(db, body.mode)
        return {"id": result["id"], "sha256": result["sha256"], "coverage": result["coverage"]}

    @app.get("/api/releases/{release_id}.{extension}")
    def download_release(release_id: str, extension: str):
        with db.session() as session:
            row = session.get(Release, release_id)
            if row is None:
                raise HTTPException(404, "Release not found")
            payload = {"id": row.id, "sha256": row.sha256, **row.document}
        if extension == "json":
            return JSONResponse(
                payload, headers={"Content-Disposition": f'attachment; filename="hogline-{release_id}.json"'}
            )
        if extension == "csv":
            return Response(
                csv_export(payload),
                media_type="text/csv",
                headers={"Content-Disposition": f'attachment; filename="hogline-{release_id}.csv"'},
            )
        raise HTTPException(404, "Use json or csv")

    static = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static), name="static")

    @app.get("/")
    def index():
        return FileResponse(static / "index.html")

    return app
