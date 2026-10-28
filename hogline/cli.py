import argparse
import json
from pathlib import Path
from uuid import uuid4

from hogline.adapters.sources import import_local, import_youtube, inventory
from hogline.analytics.exports import csv_export, snapshot
from hogline.config import Settings
from hogline.db import Database
from hogline.domain.catalog import catalog
from hogline.domain.models import ScanRequest
from hogline.evaluation.datasets import freeze_dataset
from hogline.evaluation.labels import labels_catalog, save_labels
from hogline.evaluation.metrics import evaluate_dataset
from hogline.evaluation.models import DatasetCreate, EvaluationRequest, LabelEdit
from hogline.orchestration.adaptive import AdaptiveRequest, create_adaptive_batch, preview_adaptive
from hogline.orchestration.automatic import AutomaticRequest, create_automatic_batch, preview_automatic
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
    PlanRequest,
    batches,
    control_batch,
    create_batch,
    preview_plan,
)
from hogline.orchestration.windows import WindowRequest, enqueue_window
from hogline.orchestration.worker import enqueue, retry, run_one
from hogline.perception.broadcast import analyze_broadcast
from hogline.perception.pipeline import analyze_cached


def main():
    parser = argparse.ArgumentParser(description="Hogline curling archive workbench")
    parser.add_argument("--data-dir", default=None)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="Initialize the local catalog without fetching media")
    source = commands.add_parser("import", help="Import metadata only; no video download")
    source.add_argument("url")
    local = commands.add_parser("import-local", help="Register a local recording without copying it")
    local.add_argument("path")
    local.add_argument("--sheet", type=int, required=True)
    listing = commands.add_parser("inventory", help="Bounded metadata-only channel/playlist discovery")
    listing.add_argument("url")
    listing.add_argument("--limit", type=int, default=12)
    discovery = commands.add_parser("discover", help="Create a durable metadata-only discovery feed")
    discovery.add_argument("url")
    discovery.add_argument("--page-size", type=int, default=12)
    discovery.add_argument("--overlap", type=int, default=3)
    discovery.add_argument("--pages", type=int, default=2)
    discovery.add_argument(
        "--schedule", action="store_true", help="Reconcile while the local server is running"
    )
    commands.add_parser("discovery", help="Inspect listing checkpoints and recent page runs")
    discovery_control = commands.add_parser("discovery-control")
    discovery_control.add_argument("feed_id")
    discovery_control.add_argument(
        "action", choices=["next", "head", "restart", "pause", "resume", "configure"]
    )
    discovery_control.add_argument("--scheduled", action=argparse.BooleanOptionalAction, default=None)
    discovery_work = commands.add_parser(
        "discovery-work", help="Process bounded metadata pages; never fetch media"
    )
    discovery_work.add_argument("--once", action="store_true")
    commands.add_parser("catalog")
    commands.add_parser("analyze", help="Run OpenCV on cached evidence; never downloads video")
    broadcast = commands.add_parser("analyze-broadcast", help="Resume OCR on pending detailed frames")
    broadcast.add_argument("--limit", type=int, default=120)
    broadcast.add_argument("--max-seconds", type=float, default=120)
    commands.add_parser("labels", help="List saved label views and recording/day groups")
    label_import = commands.add_parser("label-import", help="Import a revisioned label-edit JSON document")
    label_import.add_argument("path")
    freeze = commands.add_parser(
        "freeze-dataset", help="Freeze exact label revisions and whole-group partitions"
    )
    freeze.add_argument("path", help="DatasetCreate JSON document")
    evaluation = commands.add_parser("evaluate", help="Evaluate a detector on a frozen dataset partition")
    evaluation.add_argument("dataset_id")
    evaluation.add_argument("--partition", choices=["train", "validation", "test"], default="test")
    evaluation.add_argument("--model", default=None)
    evaluation.add_argument("--output", default=None)
    scan = commands.add_parser("scan", help="Enqueue a sparse, on-demand evidence scan")
    scan.add_argument("source_id")
    scan.add_argument("--start", type=float, default=0)
    scan.add_argument("--end", type=float, required=True)
    scan.add_argument("--interval", type=float, default=10)
    scan.add_argument("--tier", choices=["coarse", "evidence"], default="coarse")
    scan.add_argument("--max-frames", type=int, default=120)
    scan.add_argument("--max-evidence-mb", type=int, default=200)
    window = commands.add_parser(
        "window", help="Decode a short detailed interval once; retain dense observations"
    )
    window.add_argument("source_id")
    window.add_argument("--start", type=float, required=True)
    window.add_argument("--end", type=float, required=True)
    window.add_argument("--interval", type=float, default=0.5)
    window.add_argument("--max-frames", type=int, default=120)
    window.add_argument("--max-evidence-mb", type=int, default=64)
    window.add_argument("--max-worker-seconds", type=int, default=120)
    window.add_argument(
        "--new-run", action="store_true", help="Create a fresh run while preserving previous output"
    )
    plan = commands.add_parser("plan", help="Preview bounded recording chunks; no video is fetched")
    plan.add_argument("path", help="PlanRequest JSON file")
    plan.add_argument("--enqueue", action="store_true", help="Create a durable batch from the plan")
    adaptive = commands.add_parser(
        "adaptive-plan", help="Select short windows from activity, with quiet audits"
    )
    adaptive.add_argument("path", help="AdaptiveRequest JSON file")
    adaptive.add_argument("--enqueue", action="store_true")
    automatic = commands.add_parser("automatic-plan", help="Plan one bounded scan, detail and OCR campaign")
    automatic.add_argument("path", help="AutomaticRequest JSON file")
    automatic.add_argument("--enqueue", action="store_true")
    commands.add_parser("batches", help="Inspect durable plans and actual execution costs")
    control = commands.add_parser("batch-control", help="Pause, resume, or cancel a batch")
    control.add_argument("batch_id")
    control.add_argument("action", choices=["pause", "resume", "cancel"])
    control.add_argument("--max-image-growth-mb", type=int)
    control.add_argument("--max-worker-seconds", type=int)
    worker = commands.add_parser("work", help="Process queued jobs (bounded retries; no automatic backfill)")
    worker.add_argument("--once", action="store_true")
    retry_parser = commands.add_parser("retry")
    retry_parser.add_argument("job_id")
    serve = commands.add_parser("serve", help="Start the local review app and one worker")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--no-worker", action="store_true")
    export = commands.add_parser("export", help="Create an immutable dataset release")
    export.add_argument("path")
    export.add_argument("--mode", choices=["confirmed", "exploratory"], default="confirmed")
    args = parser.parse_args()
    settings = Settings.load(args.data_dir)
    db = Database(settings)
    try:
        match args.command:
            case "init":
                result = {"data_dir": str(settings.data_dir), "status": "ready"}
            case "import":
                result = import_youtube(db, args.url)
            case "import-local":
                result = import_local(db, args.path, args.sheet)
            case "inventory":
                result = inventory(db, args.url, args.limit)
            case "discover":
                result = create_feed(
                    db,
                    FeedCreate(
                        url=args.url,
                        page_size=args.page_size,
                        overlap=args.overlap,
                        backfill_pages_per_cycle=args.pages,
                        scheduled=args.schedule,
                    ),
                )
            case "discovery":
                result = {"feeds": feeds(db)}
            case "discovery-control":
                result = control_feed(
                    db, args.feed_id, FeedControl(action=args.action, scheduled=args.scheduled)
                )
            case "discovery-work":
                while (result := run_discovery_once(db)) is not None:
                    print(json.dumps(result))
                    if args.once:
                        break
                return
            case "catalog":
                result = catalog(db)
            case "analyze":
                result = analyze_cached(db)
            case "analyze-broadcast":
                result = analyze_broadcast(db, limit=args.limit, max_seconds=args.max_seconds)
            case "labels":
                result = labels_catalog(db)
            case "label-import":
                result = save_labels(db, LabelEdit.model_validate_json(Path(args.path).read_text()))
            case "freeze-dataset":
                frozen = freeze_dataset(db, DatasetCreate.model_validate_json(Path(args.path).read_text()))
                result = {"id": frozen["id"], "sha256": frozen["sha256"], "records": len(frozen["records"])}
            case "evaluate":
                if args.output and Path(args.output).exists():
                    raise ValueError("Evaluation destination exists; choose a new filename")
                result = evaluate_dataset(
                    db,
                    EvaluationRequest(
                        dataset_id=args.dataset_id, partition=args.partition, model_version=args.model
                    ),
                )
                if args.output:
                    Path(args.output).write_text(json.dumps(result, indent=2))
                    result = {
                        "id": result["id"],
                        "summary": result["summary"],
                        "path": str(Path(args.output).resolve()),
                    }
            case "scan":
                result = enqueue(
                    db,
                    ScanRequest(
                        source_id=args.source_id,
                        start_seconds=args.start,
                        end_seconds=args.end,
                        interval_seconds=args.interval,
                        acquisition_tier=args.tier,
                        max_frames=args.max_frames,
                        max_evidence_mb=args.max_evidence_mb,
                    ),
                )
            case "plan":
                request = PlanRequest.model_validate_json(Path(args.path).read_text())
                result = create_batch(db, request) if args.enqueue else preview_plan(db, request)
            case "automatic-plan":
                request = AutomaticRequest.model_validate_json(Path(args.path).read_text())
                result = (
                    create_automatic_batch(db, request) if args.enqueue else preview_automatic(db, request)
                )
            case "adaptive-plan":
                request = AdaptiveRequest.model_validate_json(Path(args.path).read_text())
                result = create_adaptive_batch(db, request) if args.enqueue else preview_adaptive(db, request)
            case "window":
                result = enqueue_window(
                    db,
                    WindowRequest(
                        source_id=args.source_id,
                        start_seconds=args.start,
                        end_seconds=args.end,
                        interval_seconds=args.interval,
                        max_frames=args.max_frames,
                        max_evidence_mb=args.max_evidence_mb,
                        max_worker_seconds=args.max_worker_seconds,
                        run_nonce=str(uuid4()) if args.new_run else None,
                    ),
                )
            case "batches":
                result = batches(db)
            case "batch-control":
                result = control_batch(
                    db,
                    args.batch_id,
                    BatchControl(
                        action=args.action,
                        max_image_growth_mb=args.max_image_growth_mb,
                        max_worker_seconds=args.max_worker_seconds,
                    ),
                )
            case "work":
                while (result := run_one(db)) is not None:
                    print(json.dumps(result))
                    if args.once:
                        break
                return
            case "retry":
                result = retry(db, args.job_id)
            case "serve":
                import uvicorn

                from hogline.review.app import create_app

                uvicorn.run(
                    create_app(settings, start_worker=not args.no_worker), host="127.0.0.1", port=args.port
                )
                return
            case "export":
                path = Path(args.path)
                if path.exists():
                    raise ValueError("Export destination exists; choose a new filename for this release")
                result = snapshot(db, args.mode)
                path.write_text(csv_export(result) if path.suffix == ".csv" else json.dumps(result, indent=2))
                result = {"id": result["id"], "path": str(path.resolve()), "sha256": result["sha256"]}
        print(json.dumps(result, indent=2))
    except (ValueError, OSError) as exc:
        parser.exit(1, f"hogline: {exc}\n")
