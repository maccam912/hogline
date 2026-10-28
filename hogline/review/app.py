import hashlib
import io
import shutil
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from PIL import Image
from pydantic import Field
from sqlalchemy import select
from starlette.middleware.trustedhost import TrustedHostMiddleware

from hogline import __version__
from hogline.adapters.sources import import_youtube
from hogline.analytics.exports import csv_export, snapshot
from hogline.config import Settings
from hogline.db import (
    AnalysisWindow,
    Database,
    DiscoveryRun,
    EvaluationDataset,
    EvaluationRun,
    Evidence,
    FrameLabels,
    FrameObservation,
    LabelRevision,
    Release,
    Revision,
    Source,
)
from hogline.domain.catalog import Conflict, catalog_in_session, save_game
from hogline.domain.models import GameEdit, Model, ScanRequest
from hogline.domain.reconciliation import (
    ReconcileCommit,
    ReconcileRequest,
    UndoReconciliation,
    commit_reconciliation,
    preview_reconciliation,
    undo_reconciliation,
)
from hogline.evaluation.datasets import dataset_catalog, freeze_dataset
from hogline.evaluation.labels import label_id, labels_catalog, save_labels
from hogline.evaluation.metrics import evaluate_dataset, run_catalog
from hogline.evaluation.models import DatasetCreate, EvaluationRequest, LabelEdit
from hogline.media.windows import VERSION as DECODER_VERSION
from hogline.orchestration.adaptive import (
    AdaptiveRequest,
    AdaptiveStart,
    create_adaptive_batch,
    preview_adaptive,
)
from hogline.orchestration.automatic import (
    AutomaticRequest,
    AutomaticStart,
    create_automatic_batch,
    preview_automatic,
)
from hogline.orchestration.discovery import (
    FeedControl,
    FeedCreate,
    control_feed,
    create_feed,
    feeds,
    run_discovery_once,
)
from hogline.orchestration.plans import (
    BatchControl,
    BatchStart,
    PlanRequest,
    batches_in_session,
    control_batch,
    create_batch,
    preview_plan,
)
from hogline.orchestration.windows import WindowRequest, enqueue_window
from hogline.orchestration.worker import enqueue, jobs_in_session, retry, run_one
from hogline.perception.board_locator import board_locations_in_session
from hogline.perception.broadcast import analyze_broadcast, broadcast_catalog_in_session
from hogline.perception.ocr import normalized_crop
from hogline.perception.pipeline import analyze_cached, observations_in_session
from hogline.perception.stones import VERSION as DETECTOR_VERSION
from hogline.perception.temporal import VERSION as WINDOW_VERSION
from hogline.perception.visibility import VERSION as VISIBILITY_VERSION
from hogline.reconstruction.boundaries import boundary_links


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
            discovered = run_discovery_once(db)
            if run_one(db) is None and discovered is None:
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
        with db.snapshot_session() as session:
            data = catalog_in_session(session)
            broadcast = broadcast_catalog_in_session(session, db.settings.data_dir)
            return {
                **data,
                "app_version": __version__,
                "jobs": jobs_in_session(session),
                "batches": batches_in_session(session),
                "observations": observations_in_session(session),
                "board_locations": board_locations_in_session(session),
                "window_version": WINDOW_VERSION,
                "window_detector_version": DETECTOR_VERSION,
                "window_visibility_version": VISIBILITY_VERSION,
                "window_decoder_version": DECODER_VERSION,
                "broadcast": broadcast,
                "source_boundary_links": boundary_links(
                    data["sources"],
                    broadcast["observations"],
                    broadcast["version"],
                    {e["id"] for e in data["evidence"]},
                ),
            }

    @app.post("/api/perception/analyze")
    def analyze():
        return {"observations": analyze_cached(db)}

    @app.post("/api/windows")
    def create_window(body: WindowRequest):
        return enqueue_window(db, body)

    @app.get("/api/windows/{window_id}/observations.parquet")
    def window_observations(window_id: str):
        with db.session() as session:
            row = session.get(AnalysisWindow, window_id)
            if row is None:
                raise HTTPException(404, "Analysis window not found")
            asset = row.document["parquet"]
        path = (db.settings.data_dir / asset["path"]).resolve()
        if not path.is_relative_to(db.settings.data_dir / "dense"):
            raise ValueError("Dense artifact path is outside its data directory")
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != asset["sha256"]:
            raise HTTPException(404, "Dense observations are missing or corrupt; use Reanalyze this window")
        return FileResponse(
            path, media_type="application/vnd.apache.parquet", filename=f"hogline-{window_id}.parquet"
        )

    @app.get("/api/labels/{evidence_id}/{view}")
    def get_labels(evidence_id: str, view: str):
        with db.session() as session:
            row = session.get(FrameLabels, label_id(evidence_id, view))
            return {"id": row.id, "revision": row.revision, **row.document} if row else None

    @app.put("/api/labels/{evidence_id}/{view}")
    def put_labels(evidence_id: str, view: str, body: LabelEdit):
        if body.labels.evidence_id != evidence_id or body.labels.view != view:
            raise ValueError("Label target does not match the evidence/view URL")
        return save_labels(db, body)

    @app.get("/api/labels/{evidence_id}/{view}/history")
    def label_history(evidence_id: str, view: str):
        with db.session() as session:
            return [
                {
                    "revision": r.number,
                    "author": r.author,
                    "reason": r.reason,
                    "created_at": r.created_at,
                    "labels": r.document,
                }
                for r in session.scalars(
                    select(LabelRevision)
                    .where(LabelRevision.labels_id == label_id(evidence_id, view))
                    .order_by(LabelRevision.number.desc())
                )
            ]

    @app.get("/api/evaluation")
    def evaluation_state():
        labels = labels_catalog(db)
        with db.session() as session:
            versions = list(
                session.scalars(
                    select(FrameObservation.model_version)
                    .where(FrameObservation.model_version.like("opencv-hsv-handles-%"))
                    .distinct()
                )
            )
        return {
            "groups": labels["groups"],
            "suggested_source_groups": labels["suggested_source_groups"],
            "datasets": dataset_catalog(db),
            "runs": run_catalog(db),
            "default_model_version": DETECTOR_VERSION,
            "model_versions": sorted(set([DETECTOR_VERSION, *versions])),
        }

    @app.post("/api/evaluation/datasets")
    def create_dataset(body: DatasetCreate):
        result = freeze_dataset(db, body)
        return {"id": result["id"], "sha256": result["sha256"], "records": len(result["records"])}

    @app.post("/api/evaluation/runs")
    def evaluate(body: EvaluationRequest):
        return evaluate_dataset(db, body)

    @app.get("/api/evaluation/{kind}/{identifier}.json")
    def evaluation_export(kind: str, identifier: str):
        table = {"datasets": EvaluationDataset, "runs": EvaluationRun}.get(kind)
        if table is None:
            raise HTTPException(404, "Use datasets or runs")
        with db.session() as session:
            row = session.get(table, identifier)
            if row is None:
                raise HTTPException(404, "Evaluation artifact not found")
            return JSONResponse(
                {"id": row.id, "sha256": row.sha256, **row.document},
                headers={"Content-Disposition": f'attachment; filename="hogline-{kind}-{identifier}.json"'},
            )

    @app.post("/api/perception/broadcast")
    def read_broadcast(body: BroadcastRequest):
        with db.session() as session:
            ids = list(session.scalars(select(Evidence.id).where(Evidence.source_id == body.source_id)))
        if not ids:
            raise ValueError("Cache a source frame before reading its clock or board")
        if not shutil.which("tesseract"):
            raise ValueError("Install Tesseract OCR to read retained clock/scoreboard frames locally")
        return analyze_broadcast(db, ids)

    @app.get("/api/board-locations/{observation_id}/{index}.png")
    def board_location_image(observation_id: str, index: int):
        with db.session() as session:
            row = session.get(FrameObservation, observation_id)
            if row is None or row.document.get("kind") != "board_location_observation":
                raise HTTPException(404, "Board location not found")
            crops = row.document["crops"]
            if not 0 <= index < len(crops):
                raise HTTPException(404, "Board location crop not found")
            frame = session.get(Evidence, row.evidence_id)
            asset = frame.document["image"] if frame else None
            path = db.settings.data_dir / asset["path"] if asset else None
            if (
                path is None
                or not path.is_file()
                or hashlib.sha256(path.read_bytes()).hexdigest() != row.document["source_image_sha256"]
            ):
                raise HTTPException(410, "Original board evidence missing or corrupt; use its YouTube link")
            image = normalized_crop(Image.open(path).convert("RGB"), crops[index]["normalized_bounds"])
            if hashlib.sha256(image.tobytes()).hexdigest() != crops[index]["rgb_sha256"]:
                raise HTTPException(410, "Derived board crop checksum mismatch")
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            return Response(buffer.getvalue(), media_type="image/png")

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

    @app.get("/api/discovery")
    def get_discovery():
        return {"feeds": feeds(db)}

    @app.get("/api/discovery/runs/{run_id}")
    def get_discovery_run(run_id: str):
        with db.session() as session:
            row = session.get(DiscoveryRun, run_id)
            if row is None:
                raise HTTPException(404, "Discovery page not found")
            return {"id": row.id, "state": row.state, "attempts": row.attempts, **row.document}

    @app.post("/api/discovery/feeds")
    def add_discovery_feed(body: FeedCreate):
        return create_feed(db, body)

    @app.post("/api/discovery/feeds/{feed_id}/control")
    def change_discovery_feed(feed_id: str, body: FeedControl):
        return control_feed(db, feed_id, body)

    @app.post("/api/scans")
    def add_scan(body: ScanRequest):
        return enqueue(db, body)

    @app.post("/api/automatic/preview")
    def automatic_preview(body: AutomaticRequest):
        return preview_automatic(db, body)

    @app.post("/api/automatic/batches")
    def automatic_start(body: AutomaticStart):
        return create_automatic_batch(db, body.request, body.expected_plan_sha256)

    @app.post("/api/adaptive/preview")
    def adaptive_preview(body: AdaptiveRequest):
        return preview_adaptive(db, body)

    @app.post("/api/adaptive/batches")
    def adaptive_start(body: AdaptiveStart):
        return create_adaptive_batch(db, body.request, body.expected_plan_sha256)

    @app.post("/api/plans/preview")
    def preview_processing_plan(body: PlanRequest):
        return preview_plan(db, body)

    @app.post("/api/batches")
    def start_processing_batch(body: BatchStart):
        return create_batch(db, body.request, body.expected_plan_sha256)

    @app.post("/api/batches/{batch_id}/control")
    def change_batch(batch_id: str, body: BatchControl):
        return control_batch(db, batch_id, body)

    @app.post("/api/jobs/{job_id}/retry")
    def retry_job(job_id: str):
        return retry(db, job_id)

    @app.put("/api/games/{game_id}")
    def edit_game(game_id: str, body: GameEdit):
        if body.game.id != game_id:
            raise ValueError("Game ID does not match the URL")
        return save_game(db, body)

    @app.post("/api/reconciliations/preview")
    def reconcile_preview(body: ReconcileRequest):
        return preview_reconciliation(db, body)

    @app.post("/api/reconciliations")
    def reconcile_publish(body: ReconcileCommit):
        return commit_reconciliation(db, body)

    @app.post("/api/reconciliations/{identifier}/undo")
    def reconcile_undo(identifier: str, body: UndoReconciliation):
        return undo_reconciliation(db, identifier, body)

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
