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
2. **Scan a section** seeks individual source frames on demand. The UI and CLI default to coarse activity frames, up to 640×360, sampled every ten seconds. No complete stream is stored.
3. Inspect the activity timeline, then request a **Detailed frame** for OpenCV handle candidates and scoreboard reading. Detailed acquisition uses a separate cache and the best available encode up to 1080p. Use **Check on YouTube** to verify the source timestamp. Switch between composite and overhead seed crops; toggle the detection overlay.
4. Review game spans, link continuation/overlap recordings, and record end scores with supporting frames. Mark boundaries separately from coverage, game completion, and the final result.
5. **Export dataset** creates an immutable JSON or CSV snapshot from one consistent database view. Confirmed mode excludes unreviewed games/scores; exploratory mode includes proposals explicitly. Unknown scores remain null. JSON includes input manifests and explicit legacy lineage gaps; see [release provenance](docs/RELEASES.md).
6. **Label an overhead view** records visible handle centers, partial visibility, ignored occlusions, and known non-stone regions that count false detections. **Evaluate CV** freezes whole recording/day groups and exact label revisions, then compares detector versions with explicit misses, extras, and coverage. See the [evaluation guide](docs/EVALUATION.md).
7. **Choose detail windows automatically** selects bounded intervals from activity and transitions, plus quiet audits. Preview, start, and pause a batch with shared budgets; see [adaptive selection](docs/ADAPTIVE.md).
8. **Analyze a short window** decodes up to 60 seconds once, compares repeated overhead observations, and retains a few image anchors plus a downloadable Parquet file. Persistence, motion, clipped candidates, and possible foreground obstruction stay visible. See [detailed windows](docs/WINDOWS.md).

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
# Request original selected-encode detail at a particular moment.
uv run hogline scan SOURCE_ID --start 1815 --end 1816 --interval 1 --tier evidence
# Decode one short interval for repeated overhead observations.
uv run hogline window SOURCE_ID --start 1800 --end 1820 --interval 0.5

# Rerun the detector on retained evidence without accessing any video.
uv run hogline analyze
uv run hogline analyze-broadcast --limit 120 --max-seconds 120

# Releases include source spans, exact interpretation revisions, and timestamp links.
uv run hogline export /absolute/path/pilot.json --mode exploratory
```

Clock/board OCR passes select pending detailed frames before applying their limit. Completed observations are preserved if the time or storage budget stops a pass; the response reports processed and remaining frames. Repeat the command or **Read cached frames on this sheet** to continue. A model/configuration change creates a new backlog without replacing earlier observations.

The `inventory` command reads a bounded first page of up to 50 entries. **Discover sources** and `hogline discover` add durable overlapping-page checkpoints, bounded backfill cycles, optional reconciliation schedules, and retrievable page snapshots. See [incremental discovery](docs/DISCOVERY.md). Video discovery/import never enqueues media processing automatically.

Channel listings are tracked separately from full recording metadata. A later sparse listing cannot discard a known duration, replace a full manifest hash, or reset stored clock/event-date fields. Listing changes set `metadata_refresh_needed`; importing the recording URL again performs a full refresh. Every curated input snapshot remains content-hashed and excludes signed media URLs and request credentials.

**Plan recording** previews a long recording and admits a limited batch of overlapping chunks. Pause, resume, image-growth limits, cumulative worker time, and per-attempt accounting are available from the workbench and CLI. Completed chunks are reused when previewing the remaining work. See [bounded processing](docs/PROCESSING.md) for the plan format and tested limits.

Each sparse scan is limited to 20 source minutes, a declared frame count, an evidence-byte budget, and a worker-time budget (default ten worker minutes; a currently running sparse decoder may take up to its 45-second timeout to return). Detailed windows have separate 60-second/240-frame limits. The shared retained-data cache defaults to 2 GiB, configurable with `HOGLINE_MAX_CACHE_MB`, and counts evidence PNGs and dense Parquet files. These are retained-data and worker limits, **not measured network-byte quotas**: remote seeks can fetch surrounding codec/keyframe data. Successful sparse frame caches survive a failed attempt. Retry failed jobs with `uv run hogline retry JOB_ID`; retries are bounded to three attempts. Repeating an identical successful job reuses it.

Every frame stores its original integer PTS and rational timebase, source-relative seconds, SHA-256, crop provenance, and a YouTube `&t=…s` link. YouTube playback links use whole seconds; the retained PTS is the precise reference. Source links survive removal of a local cache. Upload dates, dates parsed from titles, and recording clocks are separate fields; wall-clock mapping remains unresolved.

Sparse scans now publish activity separately from games. The timeline joins motion runs across contiguous work units, counts only each chunk's central ownership region, and leaves gaps unknown. A sampled span means the requested range has discrete samples, not continuous observation. Motion runs can seed a game review draft; saving a game requires a separate decision. Earlier pilot game proposals remain in local revision history. Coarse frames are excluded from handle/house and scoreboard analysis. Cached originals survive crop-budget failures; changing source metadata cannot silently reuse an acquisition pinned to the old manifest.

Configure `HOGLINE_DATA_DIR` or use `hogline --data-dir /absolute/path …` to isolate datasets. Keep the catalog and evidence together when backing up; local originals are registered by reference and need their own backup.

To revise camera crops, copy/edit `profiles/wausau-v1.json` and set `HOGLINE_PROFILE` to its absolute path before starting the server. Jobs retain the exact profile and its hash. Profile changes produce new evidence versions rather than silently changing old crop measurements.

## Automation in this build

- Sparse panel motion produces **activity candidates**, with advertisements excluded. It cannot yet distinguish games, practice, maintenance, adjacent-sheet movement, or immediate back-to-back games reliably.
- An **OpenCV HSV/contour baseline** proposes red/yellow handle positions in overhead seed crops. It stores crop pixels and uncalibrated shape scores, not stone footprints or metric coordinates. Rejected size/shape candidates remain inspectable with their reasons. A `StoneDetector` interface permits replacement with a trained detector.
- Per-view blue-ring ellipse fits check house framing at the actual source resolution. They remain image-space observations, without surveyed dimensions or physical A/B labels.
- Short detailed windows preserve original PTS and store repeated detections in Parquet instead of a SQL row for every frame. A conservative temporal check proposes persistent handle configurations, splitting on motion, changed positions/colors, gaps, clipped candidates, large dark regions, blue-ring interruptions, and missing ring references that prevent a usable view. This is an experimental visibility gate, not a person detector or a verified settled-house state.
- **Local Tesseract OCR** reads the broadcast date/time and traditional boards using a scoped template or a checked fixed-header registration. Three consecutive consistent card-row observations can propose end-point differences from cumulative columns. Initial row totals, blank ends, storage markers, winners, and unreadable cards are not invented. Score proposals can populate a game review draft; saving and confirmation remain separate decisions. Install Tesseract to enable this stage.
- **Automatic board location** measures red/header/yellow geometry independently on each sheet and shows a source-linked crop. It abstains on missing or competing bands; columns and card meanings remain unverified. See [board location](docs/BOARD_LOCATION.md) for the eight-sheet checks and limits.
- Recording-scoped scoreboard templates check a retained fixed-header reference and can propose a small verified crop translation. Competing clock OCR readings remain visible; a title never supplies missing date digits. See [recording links and layout checks](docs/RECORDING_LINKS.md).
- Clock hypotheses retain AM/PM and DST alternatives. Offset segments require consecutive supporting frames and split at gaps or contradictory reads. A title date never replaces an unreadable overlay date.
- Near-boundary clock and posted-board agreement can propose a recording continuation with an explicit missing interval and links to both sources. Comparison can seed a partial multi-source game draft; it creates no accepted game or result.
- Observations are versioned independently of human interpretations. A new detector can analyze retained frames without fetching media or overwriting reviewed scores.
- The labeling studio and frozen evaluation datasets retain reviewer provenance, original crop coordinates, visibility, ignored areas, group splits, and revision history. Only complete reviewed views enter detection metrics; model seeds cannot become their own ground truth. Reports separate sheets, resolutions, and label provenance, and never authorize score acceptance.
- All source/game links are many-to-many. Job keys, stable evidence IDs, unique constraints, leases, and transactional publication prevent repeat jobs from duplicating logical output. Overlapping proposals are conservatively associated with existing games and still require review.
- Optimistic revision checks reject stale edits. Every accepted edit keeps an author, reason, prior snapshot, and evidence references. Exports freeze the interpretation revisions and metric eligibility rules.

**Still to build:** calibrated stone footprints/tracking, broader scoreboard/layout validation, stronger game/end segmentation, automatic score reconciliation and acceptance gates, a representative independently labeled evaluation corpus, split/merge reconciliation, historical rosters, compacted season-scale dense datasets, automated full-metadata refresh and season-scale discovery validation, and production operations/access control. No final results, named identities, physical A/B mappings, or archive-wide accuracy rates are accepted automatically by this pilot. A confirmed real overlap/continuation pair and a fully labeled long day remain validation work; synthetic fixtures currently exercise those relationships.

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
