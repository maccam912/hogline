# Hogline

An evidence-first curling archive. This is the first local implementation of [DESIGN.md](DESIGN.md), focused on metadata-first ingestion, bounded processing, automated observations, and revisable game records.

## Run it

Requires Python 3.12+, [uv](https://docs.astral.sh/uv/), [FFmpeg/ffprobe](https://ffmpeg.org/), and [yt-dlp](https://github.com/yt-dlp/yt-dlp) on `PATH`.

```sh
uv sync --group dev
uv run hogline serve
```

Open **http://127.0.0.1:8765**. The server starts one bounded worker. Data is kept in `data/`, which is excluded from version control. The web app is a local operator tool; leave it bound to loopback.

1. **Add recording** imports YouTube metadata without downloading video.
2. **Scan a section** seeks individual source frames on demand. The default is ten minutes sampled every ten seconds. No complete stream is stored.
3. Inspect activity proposals and OpenCV handle candidates. Use **Check on YouTube** to open the precise source timestamp. Switch between composite and overhead seed crops; toggle the detection overlay.
4. Review game spans, link continuation/overlap recordings, and record end scores with supporting frames. Mark boundaries separately from coverage, game completion, and the final result.
5. **Export dataset** creates an immutable JSON or CSV snapshot. Confirmed mode excludes unreviewed games/scores; exploratory mode includes proposals explicitly. Unknown scores remain null.
6. **Label an overhead view** records visible handle centers, partial visibility, and ignored occlusions. **Evaluate CV** freezes whole recording/day groups and exact label revisions, then compares detector versions with explicit misses, extras, and coverage. See the [evaluation guide](docs/EVALUATION.md).

The default catalog is SQLite for a zero-service local pilot. SQLAlchemy models support a PostgreSQL connection through `HOGLINE_DATABASE_URL`; the PostgreSQL runtime has **not** been exercised yet. Install its driver with `uv sync --extra postgres`. Schema creation currently uses `create_all`; use explicit migrations before evolving a deployed database.

## Metadata first, media on demand

```sh
# These import metadata only.
uv run hogline import 'https://www.youtube.com/watch?v=es9urvYvLiE'
uv run hogline import 'https://www.youtube.com/watch?v=976gtQn17P0'
uv run hogline inventory 'https://www.youtube.com/channel/UCpQIZCWomj9fy287uBS3cxA/streams' --limit 12
uv run hogline catalog

# Register a local original by content hash, without copying it.
uv run hogline import-local /absolute/path/recording.mp4 --sheet 5

# Copy a source ID from the catalog. Times below are source seconds.
uv run hogline scan SOURCE_ID --start 1800 --end 2400 --interval 10 --max-evidence-mb 200
uv run hogline work --once

# Rerun the detector on retained evidence without accessing any video.
uv run hogline analyze
uv run hogline analyze-broadcast

# Releases include source spans, exact interpretation revisions, and timestamp links.
uv run hogline export /absolute/path/pilot.json --mode exploratory
```

Inventory is bounded to 50 entries per request. It is a first page, **not** a complete seasonal backfill or a discovery checkpoint system. Video import never enqueues processing automatically.

Each scan is limited to 20 source minutes, a declared frame count, an evidence-byte budget, and a worker-time budget (default ten worker minutes; a currently running decoder may take up to its 45-second timeout to return). The shared evidence cache defaults to 2 GiB, configurable with `HOGLINE_MAX_CACHE_MB`. These are retained-image and worker limits, **not measured network-byte quotas**: remote seeks can fetch surrounding codec/keyframe data. Successful frame caches survive a failed attempt. Retry failed jobs with `uv run hogline retry JOB_ID`; retries are bounded to three attempts. Repeating an identical successful job reuses it.

Every frame stores its original integer PTS and rational timebase, source-relative seconds, SHA-256, crop provenance, and a YouTube `&t=…s` link. YouTube playback links use whole seconds; the retained PTS is the precise reference. Source links survive removal of a local cache. Upload dates, dates parsed from titles, and recording clocks are separate fields; wall-clock mapping remains unresolved.

Configure `HOGLINE_DATA_DIR` or use `hogline --data-dir /absolute/path …` to isolate datasets. Keep the catalog and evidence together when backing up; local originals are registered by reference and need their own backup.

To revise camera crops, copy/edit `profiles/wausau-v1.json` and set `HOGLINE_PROFILE` to its absolute path before starting the server. Jobs retain the exact profile and its hash. Profile changes produce new evidence versions rather than silently changing old crop measurements.

## Automation in this build

- Sparse panel motion produces **activity candidates**, with advertisements excluded. It cannot yet distinguish games, practice, maintenance, adjacent-sheet movement, or immediate back-to-back games reliably.
- An **OpenCV HSV/contour baseline** proposes red/yellow handle positions in overhead seed crops. It stores crop pixels and uncalibrated shape scores, not stone footprints or metric coordinates. A `StoneDetector` interface permits replacement with a trained detector.
- Per-view blue-ring ellipse fits check house framing at the actual source resolution. They remain image-space observations, without surveyed dimensions or physical A/B labels.
- **Local Tesseract OCR** reads the broadcast date/time and a scoped traditional-board template. Three consecutive consistent card-row observations can propose end-point differences from cumulative columns. Initial row totals, blank ends, storage markers, winners, and unreadable cards are not invented. Score proposals can populate a game review draft; saving and confirmation remain separate decisions. Install Tesseract to enable this stage.
- Clock hypotheses retain AM/PM and DST alternatives. Offset segments require consecutive supporting frames and split at gaps or contradictory reads. A title date never replaces an unreadable overlay date.
- Observations are versioned independently of human interpretations. A new detector can analyze retained frames without fetching media or overwriting reviewed scores.
- The labeling studio and frozen evaluation datasets retain reviewer provenance, original crop coordinates, visibility, ignored areas, group splits, and revision history. Only complete reviewed views enter detection metrics; model seeds cannot become their own ground truth. Reports separate sheets, resolutions, and label provenance, and never authorize score acceptance.
- All source/game links are many-to-many. Job keys, stable evidence IDs, unique constraints, leases, and transactional publication prevent repeat jobs from duplicating logical output. Overlapping proposals are conservatively associated with existing games and still require review.
- Optimistic revision checks reject stale edits. Every accepted edit keeps an author, reason, prior snapshot, and evidence references. Exports freeze the interpretation revisions and metric eligibility rules.

**Still to build:** calibrated stone footprints/tracking, scoreboard templates for the remaining sheets/layout episodes, stronger game/end segmentation, automatic score reconciliation and acceptance gates, a representative independently labeled evaluation corpus, split/merge reconciliation, historical rosters, dense Parquet observations, incremental seasonal discovery, and production operations/access control. No final results, named identities, physical A/B mappings, or archive-wide accuracy rates are accepted automatically by this pilot. A confirmed real overlap/continuation pair and a fully labeled long day remain validation work; synthetic fixtures currently exercise those relationships.

The next automation milestone is **stable house states + end transitions + labeled evaluation**, followed by measured automatic acceptance. Human review should shrink to uncertain exceptions as precision improves. See [cross-sheet validation](docs/CROSS_SHEET.md), [initial pilot notes](docs/PILOT.md), and the [annotation guide](docs/ANNOTATION.md).

## Development

```sh
uv run pytest -q
uv run ruff check hogline tests
uv run ruff format --check hogline tests
node --check hogline/review/static/app.js
```

Tests use synthetic data and local FFmpeg fixtures, never download YouTube media. They cover multiple games/uploads, idempotent import/job execution, interrupted-job reuse, stale review edits, unknown/blank scores, score evidence validation, immutable releases, timestamp precision, cache corruption, color/shape discrimination, and evidence budgets.

The review API is documented at `/docs`. Its main routes are `GET /api/catalog`, `POST /api/sources`, `POST /api/scans`, `PUT /api/games/{id}`, `POST /api/perception/analyze`, and `POST /api/releases`.

The public repository contains text source and documentation. Local data, media, model weights, environment files, and build outputs are ignored. `python3 scripts/check_repository.py` checks staged Git blobs for private/artifact paths, binary content, files over 1 MiB, and common credential patterns; CI checks every tracked blob. Enable the local hook after cloning with `git config core.hooksPath .githooks`, and review the staged diff before publishing.
