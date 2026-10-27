import argparse
import json
from pathlib import Path

from hogline.adapters.sources import import_local, import_youtube, inventory
from hogline.analytics.exports import csv_export, snapshot
from hogline.config import Settings
from hogline.db import Database
from hogline.domain.catalog import catalog
from hogline.domain.models import ScanRequest
from hogline.orchestration.worker import enqueue, retry, run_one
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
    commands.add_parser("catalog")
    commands.add_parser("analyze", help="Run OpenCV on cached evidence; never downloads video")
    scan = commands.add_parser("scan", help="Enqueue a sparse, on-demand evidence scan")
    scan.add_argument("source_id")
    scan.add_argument("--start", type=float, default=0)
    scan.add_argument("--end", type=float, required=True)
    scan.add_argument("--interval", type=float, default=10)
    scan.add_argument("--max-frames", type=int, default=120)
    scan.add_argument("--max-evidence-mb", type=int, default=200)
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
            case "catalog":
                result = catalog(db)
            case "analyze":
                result = analyze_cached(db)
            case "scan":
                result = enqueue(
                    db,
                    ScanRequest(
                        source_id=args.source_id,
                        start_seconds=args.start,
                        end_seconds=args.end,
                        interval_seconds=args.interval,
                        max_frames=args.max_frames,
                        max_evidence_mb=args.max_evidence_mb,
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
