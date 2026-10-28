# Bounded recording plans

`Plan recording` previews an entire recording as small work units. It fetches no media and creates no jobs. The preview reports admitted chunks, previously completed work, deferred chunks, sample requests, and a rough PNG-growth estimate. A plan hash pins the source metadata, processing versions, crop profile, requested spans, and matching-job state. Starting a stale browser preview returns a conflict and requires another preview.

The default plan uses 600-second central intervals with 30 seconds of context on either side, coarse samples every ten seconds, and at most two new chunks from the browser (12 from the CLI model default). The sample grid stays aligned across boundaries. Context provides comparisons but does not own events or increase scanned central coverage. The full request can exceed the admitted batch: completing two of 61 requested chunks does not mean the ten-hour recording was processed. **Preview remaining** skips exact successful current-input units and admits the next bounded set. Changing grid/chunk parameters creates a different plan; its original frames can still be reused by timestamp and acquisition tier.

For CLI use, save a private plan file, replacing the source ID:

```json
{
  "source_ids": ["SOURCE_ID_FROM_CATALOG"],
  "start_seconds": 0,
  "chunk_seconds": 600,
  "context_seconds": 30,
  "interval_seconds": 10,
  "acquisition_tier": "coarse",
  "max_jobs": 2,
  "max_image_growth_mb": 250,
  "max_worker_seconds": 1800
}
```

Omitting `end_seconds` uses each source's known duration. Unknown duration needs an explicit end. Multi-source plans admit units in the supplied source order; they do not synchronize source clocks or reconcile overlapping uploads.

```sh
uv run hogline plan /absolute/path/private-plan.json
uv run hogline plan /absolute/path/private-plan.json --enqueue
uv run hogline batches
uv run hogline batch-control BATCH_ID pause
uv run hogline batch-control BATCH_ID resume --max-image-growth-mb 500 --max-worker-seconds 3600
uv run hogline batch-control BATCH_ID cancel
```

Pause and cancel are observed between decoding/caching stages. A decoder already running may take up to 45 seconds to return; media resolution has a separate 60-second timeout. The batch can show paused while its current execution is still stopping. Resume uses retained images and does not overwrite prior costs or interpretations. Cancelled logical work can be admitted into a new batch; the earlier batch and its execution history remain intact. Failed units require explicit retry before their batch resumes. Control stops do not consume the three failure attempts.

Every lease creates a separate execution record. It records wall time and reservations for new image bytes across successful, failed, stopped, and expired attempts. Image growth is reserved before writing, so interrupted/failed writes can conservatively overstate growth. Existing content-addressed images cost no new reservation. PNGs, crops, and repaired assets use the same shared-cache quota. A recovered expired lease is charged one additional 120-second lease as a conservative bound on time since its last heartbeat. Time limits are checked at stage boundaries, so an in-progress operation can exceed the nominal time cap; it cannot start the next frame after the cap is reached. Monetary model calls and reviewer time are not used/metered here, and network bytes are not measured. The `_mb` limits use 1,048,576 bytes per unit.

The local server runs one worker. Each batch admits one active execution at a time; logical job claims and batch revisions guard publication and accounting. Multi-host operation and the PostgreSQL runtime still need deployment validation. The current schema adds batch/execution tables through `create_all`; a deployed service needs explicit migrations and operational access control.

The activity timeline unions successful central scan spans, deduplicates overlapping target times, and excludes scans pinned to obsolete source metadata. A single detailed frame has no motion comparison, so it cannot erase a valid sparse motion sample. Activity runs retain evidence IDs and YouTube timestamps and can seed a review draft. They do not automatically create games, accepted end scores, or final results.

## Validation checkpoint

The January 16 Sheet 5 recording was previewed as 61 possible ten-minute chunks without fetching media. A separate small plan requested source 1800–1980 seconds, admitted only two 60-second central chunks with 30-second context, and deferred the third. The browser paused the first execution, waited for it to stop, then resumed with the same 8 MiB / 180-worker-second limits. Both admitted chunks completed: five distinct coarse frames, two context cache hits in the second chunk, 2,215,089 reserved image bytes, and 53.36 worker seconds across the stopped and successful attempts. No new game records or accepted results were produced. A next-plan preview, rather than an automatic backfill, handles the deferred minute.

Failure-injection tests cover dry-run side effects, aligned ownership, cached context reuse, cumulative image/time limits, mid-decode cancellation, lease expiry, stale previews/source manifests, and the HTTP controls. These are operational checks; sparse frames do not establish full-day game reconstruction accuracy.
