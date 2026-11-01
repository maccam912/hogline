import hashlib
import io
import logging
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
from hogline.analytics.actors import actors_in_session, checked_actor_result, derive_window_actors
from hogline.analytics.deliveries import (
    checked_delivery_result,
    deliveries_in_session,
    derive_window_deliveries,
)
from hogline.analytics.exports import csv_export, grouped_scores_csv, snapshot
from hogline.analytics.house_states import checked_result, derive_house_states, house_states_in_session
from hogline.analytics.moments import MomentQuery, search_moments
from hogline.analytics.tracks import derive_window_tracks, tracks_in_session
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
    WindowActors,
    WindowDeliveries,
    WindowHouseStates,
    WindowTracks,
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
from hogline.orchestration.board_refinement import (
    RefinementRequest,
    RefinementStart,
    create_refinement_batch,
    preview_refinement,
)
from hogline.orchestration.discovery import (
    FeedControl,
    FeedCreate,
    control_feed,
    create_feed,
    feeds,
    run_discovery_once,
)
from hogline.orchestration.metadata import MetadataRefreshRequest, enqueue_refresh, metadata_queue
from hogline.orchestration.plans import (
    BatchControl,
    BatchStart,
    PlanRequest,
    batches_in_session,
    control_batch,
    create_batch,
    preview_plan,
)
from hogline.orchestration.pool import WorkerPool
from hogline.orchestration.recording_campaigns import tick as tick_recording_campaigns
from hogline.orchestration.windows import RETENTION_VERSION, WindowRequest, enqueue_window
from hogline.orchestration.worker import enqueue, jobs_in_session, retry
from hogline.perception.actors import VERSION as ACTOR_VERSION
from hogline.perception.board_locator import board_locations_in_session
from hogline.perception.broadcast import analyze_broadcast, broadcast_catalog_in_session
from hogline.perception.ocr import normalized_crop
from hogline.perception.pipeline import analyze_cached, observations_in_session
from hogline.perception.stones import VERSION as DETECTOR_VERSION
from hogline.perception.temporal import VERSION as WINDOW_VERSION
from hogline.perception.tracking import VERSION as TRACKING_VERSION
from hogline.perception.visibility import VERSION as VISIBILITY_VERSION
from hogline.reconstruction.boundaries import boundary_links
from hogline.reconstruction.deliveries import VERSION as DELIVERY_VERSION
from hogline.reconstruction.house_states import VERSION as HOUSE_STATES_VERSION
from hogline.review.board_evaluation import register_board_evaluation
from hogline.review.catalog_response import CatalogResponseCache
from hogline.review.ends import register_ends
from hogline.review.examples import register_examples
from hogline.review.house_evaluation import register_house_evaluation
from hogline.review.learning import model_catalog, register_learning
from hogline.review.lineups import register_lineups
from hogline.review.players import register_players
from hogline.review.reconstructions import register_reconstructions
from hogline.review.recording_campaigns import register_campaigns
from hogline.review.recordings import register_recordings
from hogline.review.score_conflicts import conflicts_in_session, register_score_conflicts
from hogline.review.skills import register_skills
from hogline.review.workspace import overview_in_session, source_processing_in_session


class SourceImport(Model):
    url: str = Field(max_length=500)


class ReleaseRequest(Model):
    mode: str = "confirmed"


class BroadcastRequest(Model):
    source_id: str


def create_app(settings: Settings | None = None, start_worker=False):
    db = Database(settings or Settings.load())
    stop = threading.Event()
    worker_state = {"enabled": start_worker, "thread": None, "error": None}
    pool = None
    catalog_responses = CatalogResponseCache()
    overview_responses = CatalogResponseCache()
    source_responses = CatalogResponseCache(ttl=5, max_entries=4)
    boundary_responses = CatalogResponseCache(ttl=30, max_entries=4)

    def worker_loop():
        try:
            while not stop.is_set():
                tick_recording_campaigns(db)
                if run_discovery_once(db) is None:
                    stop.wait(1)
        except Exception as exc:
            worker_state["error"] = str(exc)
            logging.getLogger(__name__).exception("Local processing worker stopped")

    @asynccontextmanager
    async def lifespan(_):
        nonlocal pool
        thread = None
        if start_worker:
            pool = WorkerPool(db.settings)
            pool.start()
            thread = threading.Thread(target=worker_loop, daemon=True, name="hogline-worker")
            worker_state["thread"] = thread
            thread.start()
        try:
            yield
        finally:
            stop.set()
            if thread:
                thread.join(timeout=1)
            if pool:
                pool.close()

    app = FastAPI(title="Hogline", version=__version__, lifespan=lifespan)
    from hogline.review.screening import router as screening_router

    app.include_router(screening_router(db))
    app.state.db = db
    register_board_evaluation(app, db)
    register_house_evaluation(app, db)
    register_learning(app, db)
    register_players(app, db)
    register_lineups(app, db)
    register_skills(app, db)
    register_examples(app, db)
    register_reconstructions(app, db)
    register_score_conflicts(app, db)
    register_ends(app, db)
    register_recordings(app, db)
    register_campaigns(app, db)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])

    @app.middleware("http")
    async def local_origin(request: Request, call_next):
        origin = request.headers.get("origin")
        if request.method not in ("GET", "HEAD", "OPTIONS") and origin:
            if origin != str(request.base_url).rstrip("/"):
                return JSONResponse({"detail": "Mutations require the same local origin"}, status_code=403)
        response = await call_next(request)
        if request.method not in ("GET", "HEAD", "OPTIONS") and response.status_code < 400:
            catalog_responses.invalidate()
            overview_responses.invalidate()
            source_responses.invalidate()
            boundary_responses.invalidate()
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.exception_handler(ValueError)
    async def invalid(_, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409 if isinstance(exc, Conflict) else 422)

    @app.post("/api/moments/search")
    def find_moments(query: MomentQuery):
        return search_moments(db, query)

    def build_catalog():
        with db.snapshot_session() as session:
            data = catalog_in_session(session)
            broadcast = broadcast_catalog_in_session(session, db.settings.data_dir)
            # These documents already contain JSON values. Avoid FastAPI's recursive
            # conversion of the whole archive on the event loop before serialization.
            return JSONResponse(
                {
                    **data,
                    "app_version": __version__,
                    "worker": {
                        "enabled": worker_state["enabled"],
                        "running": bool(worker_state["thread"] and worker_state["thread"].is_alive()),
                        "error": worker_state["error"],
                        **(
                            pool.status()
                            if pool
                            else {"configured_workers": db.settings.workers, "alive_workers": 0}
                        ),
                    },
                    "jobs": jobs_in_session(session),
                    "batches": batches_in_session(session),
                    "observations": observations_in_session(session),
                    "board_locations": board_locations_in_session(session),
                    "window_version": WINDOW_VERSION,
                    "window_tracking_version": TRACKING_VERSION,
                    "window_tracks": tracks_in_session(session),
                    "window_house_states": house_states_in_session(session),
                    "house_states_version": HOUSE_STATES_VERSION,
                    "window_actors": actors_in_session(session),
                    "actor_version": ACTOR_VERSION,
                    "window_deliveries": deliveries_in_session(session),
                    "window_detector_version": DETECTOR_VERSION,
                    "window_visibility_version": VISIBILITY_VERSION,
                    "window_decoder_version": DECODER_VERSION,
                    "window_delivery_version": DELIVERY_VERSION,
                    "window_retention_version": RETENTION_VERSION,
                    "broadcast": broadcast,
                    "score_disagreements": conflicts_in_session(session, data, broadcast),
                    "source_boundary_links": boundary_links(
                        data["sources"],
                        broadcast["observations"],
                        broadcast["version"],
                        {e["id"] for e in data["evidence"]},
                    ),
                }
            )

    @app.get("/api/catalog")
    def get_catalog(request: Request):
        return catalog_responses.response(build_catalog, request.headers.get("if-none-match"))

    @app.get("/api/workspace")
    def get_workspace(request: Request):
        def load():
            with db.snapshot_session() as session:
                data = overview_in_session(session)
            return JSONResponse(
                {
                    **data,
                    "app_version": __version__,
                    "worker": {
                        "enabled": worker_state["enabled"],
                        "running": bool(worker_state["thread"] and worker_state["thread"].is_alive()),
                        "error": worker_state["error"],
                        **(
                            pool.status()
                            if pool
                            else {
                                "configured_workers": db.settings.workers,
                                "alive_workers": 0,
                            }
                        ),
                    },
                }
            )

        return overview_responses.response(load, request.headers.get("if-none-match"))

    @app.get("/api/sources/{source_id}/workspace")
    def get_source_workspace(source_id: str, request: Request):
        def load():
            with db.snapshot_session() as session:
                if session.get(Source, source_id) is None:
                    raise HTTPException(404, "Recording not found")
                data = catalog_in_session(session, source_id)
                broadcast = broadcast_catalog_in_session(session, db.settings.data_dir, source_id)
                processing = source_processing_in_session(session, source_id)
                return JSONResponse(
                    {
                        **{
                            k: v
                            for k, v in data.items()
                            if k
                            not in (
                                "sources",
                                "games",
                                "retired_games",
                                "review_tasks",
                                "reconciliations",
                            )
                        },
                        **processing,
                        "source_id": source_id,
                        "observations": observations_in_session(session, source_id),
                        "board_locations": board_locations_in_session(session, source_id),
                        "window_version": WINDOW_VERSION,
                        "window_tracking_version": TRACKING_VERSION,
                        "window_tracks": tracks_in_session(session, source_id=source_id),
                        "window_house_states": house_states_in_session(session, source_id=source_id),
                        "house_states_version": HOUSE_STATES_VERSION,
                        "window_actors": actors_in_session(session, source_id=source_id),
                        "actor_version": ACTOR_VERSION,
                        "window_deliveries": deliveries_in_session(session, source_id=source_id),
                        "window_detector_version": DETECTOR_VERSION,
                        "window_visibility_version": VISIBILITY_VERSION,
                        "window_decoder_version": DECODER_VERSION,
                        "window_delivery_version": DELIVERY_VERSION,
                        "window_retention_version": RETENTION_VERSION,
                        "broadcast": broadcast,
                        "score_disagreements": conflicts_in_session(session, data, broadcast),
                        # Cross-recording comparisons require the archive-wide endpoint.
                        "source_boundary_links": [],
                    }
                )

        return source_responses.response(load, request.headers.get("if-none-match"), key=source_id)

    @app.get("/api/sources/{source_id}/boundary-links")
    def get_source_boundary_links(source_id: str, request: Request):
        def load():
            with db.snapshot_session() as session:
                source = session.get(Source, source_id)
                if source is None:
                    raise HTTPException(404, "Recording not found")
                sources = [
                    {**s.document, "id": s.id}
                    for s in session.scalars(select(Source))
                    if s.id == source_id
                    or (
                        source.document.get("sheet") is not None
                        and s.document.get("sheet") == source.document["sheet"]
                    )
                ]
                ids = [s["id"] for s in sources]
                broadcast = broadcast_catalog_in_session(session, db.settings.data_dir, source_ids=ids)
                evidence_ids = set(session.scalars(select(Evidence.id).where(Evidence.source_id.in_(ids))))
                links = boundary_links(sources, broadcast["observations"], broadcast["version"], evidence_ids)
                return JSONResponse(
                    {
                        "source_id": source_id,
                        "links": [
                            link
                            for link in links
                            if source_id in (link["left_source_id"], link["right_source_id"])
                        ],
                    }
                )

        return boundary_responses.response(load, request.headers.get("if-none-match"), key=source_id)

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

    @app.post("/api/windows/{window_id}/tracks")
    def create_window_tracks(window_id: str):
        return derive_window_tracks(db, window_id)

    @app.post("/api/windows/{window_id}/house-states")
    def create_house_states(window_id: str):
        return derive_house_states(db, window_id)

    @app.post("/api/windows/{window_id}/actors")
    def create_window_actors(window_id: str):
        return derive_window_actors(db, window_id)

    @app.post("/api/windows/{window_id}/deliveries")
    def create_window_deliveries(window_id: str):
        return derive_window_deliveries(db, window_id)

    @app.get("/api/window-deliveries/{identifier}")
    def get_window_deliveries(identifier: str):
        with db.session() as session:
            row = session.get(WindowDeliveries, identifier)
            if row is None:
                raise HTTPException(404, "Release analysis not found")
            return checked_delivery_result(row)

    @app.get("/api/window-actors/{identifier}")
    def get_window_actors(identifier: str):
        with db.session() as session:
            row = session.get(WindowActors, identifier)
            if row is None:
                raise HTTPException(404, "Actor analysis not found")
            return checked_actor_result(row)

    @app.get("/api/window-house-states/{identifier}")
    def get_house_states(identifier: str):
        with db.session() as session:
            row = session.get(WindowHouseStates, identifier)
            if row is None:
                raise HTTPException(404, "House-state analysis not found")
            return checked_result(row)

    @app.get("/api/window-tracks/{identifier}")
    def get_window_tracks(identifier: str):
        from hogline.adapters.sources import digest

        with db.session() as session:
            row = session.get(WindowTracks, identifier)
            if row is None:
                raise HTTPException(404, "Handle fragment analysis not found")
            if digest(row.document) != row.sha256:
                raise ValueError("Stored handle fragment checksum mismatch")
            return JSONResponse(
                {"id": row.id, "sha256": row.sha256, **row.document},
                headers={
                    "Content-Disposition": f'attachment; filename="hogline-tracks-{identifier}.json"',
                },
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
            "model_versions": sorted(
                set([DETECTOR_VERSION, *versions, *[m["version"] for m in model_catalog(db)]])
            ),
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

    @app.get("/api/metadata-refresh")
    def get_metadata_refresh():
        with db.snapshot_session() as session:
            return metadata_queue(session)

    @app.post("/api/metadata-refresh")
    def refresh_metadata(body: MetadataRefreshRequest):
        return enqueue_refresh(db, body)

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

    @app.post("/api/board-refinement/preview")
    def board_refinement_preview(body: RefinementRequest):
        return preview_refinement(db, body)

    @app.post("/api/board-refinement/batches")
    def board_refinement_start(body: RefinementStart):
        return create_refinement_batch(db, body.request, body.expected_plan_sha256)

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
            from hogline.media.frames import read_image_bytes

            try:
                content = read_image_bytes(db.settings.data_dir, asset)
            except ValueError as exc:
                raise HTTPException(
                    410,
                    "Cached image unavailable or released to save space. Use its original timestamp link.",
                ) from exc
            return Response(
                content, media_type="image/png", headers={"Cache-Control": "private, max-age=3600"}
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
        if extension == "grouped-csv":
            if payload["mode"] != "exploratory":
                raise HTTPException(409, "Grouped proposed scores require an exploratory release")
            if "grouped_score_histories" not in payload:
                raise HTTPException(409, "Create a new release to include grouped score histories")
            return Response(
                grouped_scores_csv(payload),
                media_type="text/csv",
                headers={"Content-Disposition": f'attachment; filename="hogline-{release_id}-grouped.csv"'},
            )
        raise HTTPException(404, "Use json, csv or grouped-csv")

    static = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static), name="static")

    @app.get("/")
    def index():
        page = (static / "index.html").read_text()
        # An old browser cache may have seen these URLs before revalidation was
        # enabled. Content hashes also support editable local workbench builds.
        for asset in static.iterdir():
            if asset.suffix in (".js", ".css"):
                version = hashlib.sha256(asset.read_bytes()).hexdigest()[:16]
                page = page.replace(f'"/static/{asset.name}"', f'"/static/{asset.name}?v={version}"')
        return Response(page, media_type="text/html")

    return app
