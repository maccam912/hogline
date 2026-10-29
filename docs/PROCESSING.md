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

Pause and cancel are observed between decoding/caching stages. A decoder already running may take up to 45 seconds to return; media resolution has a separate 60-second timeout. The batch can show paused while its current execution is still stopping. Resume uses retained images and does not overwrite prior costs or interpretations. Cancelled logical work can be admitted into a new batch; the earlier batch and its execution history remain intact. Permanent failures and exhausted automatic retries require attention before their batch resumes. Control stops do not consume the three failure attempts.

Transient remote-video failures now retry automatically, up to three total job attempts. FFmpeg can reconnect after network disconnects, TLS/connect errors, HTTP 429 and server errors within its existing operation timeout. A failed remote frame/window read or media-URL resolution queues a fresh job attempt after 5 seconds, then 20 seconds. Each attempt resolves a fresh stream URL; sparse jobs reuse checked cached frames, and dense windows restart cleanly without publishing partial observations. Failed execution records, elapsed costs and a job-level `failure_history` remain visible. `retry_pending` and `retry_after` survive server restart; Jobs labels the analysis **Retrying automatically** during backoff. Batch priorities stay ordered, and unrelated batches can proceed during the delay.

Pause/cancel, shared cache limits and processing budgets still stop acquisition. Missing executables, changed local originals, invalid timestamps, corrupt evidence, model/source changes and other validation errors do not enter the automatic network retry path. After the third unsuccessful attempt, the job fails and its batch pauses rather than silently accepting missing evidence. A temporary local section that cannot supply a frame falls back to the original stream. Existing paused jobs are not silently resumed by a server upgrade.

Every lease creates a separate execution record. It records wall time and reservations for new image bytes across successful, failed, stopped, and expired attempts. Image growth is reserved before writing, so interrupted/failed writes can conservatively overstate growth. Existing content-addressed images cost no new reservation. PNGs, crops, and repaired assets use the same shared-cache quota. A recovered expired lease is charged one additional 120-second lease as a conservative bound on time since its last heartbeat. Time limits are checked at stage boundaries, so an in-progress operation can exceed the nominal time cap; it cannot start the next frame after the cap is reached. Monetary model calls and reviewer time are not used/metered here, and network bytes are not measured. The `_mb` limits use 1,048,576 bytes per unit.

The local server runs four separate worker processes by default. `hogline serve --workers 8` or `HOGLINE_WORKERS=8` changes this (1–32). Processes overlap network waits and run Python/native analysis on separate cores. Each batch still admits one active execution at a time, preserving its priorities and cumulative time/data limits. Different recordings/batches can run concurrently. SQLite claims reserve their write transaction before inspecting eligibility; publication, heartbeat accounting and batch controls also reserve their write transactions. Discovery runs independently of media workers. Jobs reports configured/online workers and the number of tasks actually running. Multiple active analyses are not necessarily multiple running tasks.

Sparse scans with multiple missing remote frames download a compressed section once, stream-copying the selected video without transcoding. Frame seeks then use that local file. Remux timebases are mapped exactly back to the original integer PTS and rational timebase; section-relative seek offsets never replace source timestamps. Failed/unavailable/oversize downloads fall back to the original remote-seek path. Local originals, already cached frames and single-frame requests do not need temporary acquisition. Detailed windows already decode their contiguous interval in one FFmpeg process.

`HOGLINE_WORKING_VIDEO_MB` sets temporary compressed video per worker, default 512 MiB, range 0–4096; zero disables section acquisition. Eight workers therefore allow roughly 4 GiB of working video, plus process/decoder memory. FFmpeg's size stop can overshoot slightly by a packet. Acquisition has a 120-second limit plus bounded probes, observes cancellation/time budgets, and consumes the task's existing processing time. After three seconds, the downloader compares observed throughput and the stream's average bitrate with a heuristic remote-seek allowance (1.5 seconds per remaining frame, minimum five seconds); an apparently slower download is abandoned. Average bitrate is only an estimate. The downloader also stops at the smaller of this allowance and 120 seconds. This avoids spending minutes fetching a section whose few survey frames are quicker to seek remotely.

Task completion/failure/cancellation removes its section. The server owns the workers' temporary parent directory and removes it on normal shutdown as well, including after stopping a long-running worker. An uncatchable OS kill can leave temporary files; startup removes abandoned pool directories whose owning server PID no longer exists. Retained PNG/Parquet evidence stays available for results and retries; its separate default cache cap remains 2 GiB. Increase `HOGLINE_MAX_CACHE_MB` explicitly for a larger retained archive.

Pool workers share an atomic retained-cache growth counter initialized from disk at startup. New bytes are reserved before writing across all pool processes, so parallel tasks cannot each spend the same remaining cache capacity. Failed writes or competing writes of identical assets can conservatively overstate reservations until restart. Out-of-band file deletion is also reconciled on restart. Run one server pool per data directory; independent CLI workers, multiple servers, multi-host operation and the PostgreSQL runtime still need deployment validation. The current schema adds batch/execution tables through `create_all`; a deployed service needs explicit migrations and operational access control.

The activity timeline unions successful central scan spans, deduplicates overlapping target times, and excludes scans pinned to obsolete source metadata. A single detailed frame has no motion comparison, so it cannot erase a valid sparse motion sample. Activity runs retain evidence IDs and YouTube timestamps and can seed a review draft. They do not automatically create games, accepted end scores, or final results.

## Validation checkpoint

The January 16 Sheet 5 recording was previewed as 61 possible ten-minute chunks without fetching media. A separate small plan requested source 1800–1980 seconds, admitted only two 60-second central chunks with 30-second context, and deferred the third. The browser paused the first execution, waited for it to stop, then resumed with the same 8 MiB / 180-worker-second limits. Both admitted chunks completed: five distinct coarse frames, two context cache hits in the second chunk, 2,215,089 reserved image bytes, and 53.36 worker seconds across the stopped and successful attempts. No new game records or accepted results were produced. A next-plan preview, rather than an automatic backfill, handles the deferred minute.

Failure-injection tests cover dry-run side effects, aligned ownership, cached context reuse, cumulative image/time limits, mid-decode cancellation, lease expiry, stale previews/source manifests, and the HTTP controls. These are operational checks; sparse frames do not establish full-day game reconstruction accuracy.

## Parallel processing check, October 28, 2026

The live 18-core / 64-GiB Mac had twelve active recording analyses but only one execution process. Restarting with eight workers produced eight new simultaneous executions across independent recordings; frame counters advanced together without new failed jobs during the check. A real eighteen-minute section download exceeded 120 seconds while three remote frame seeks took 2.05/1.96/2.04 seconds. The adaptive path abandoned the same slow section in 3.91 seconds and left zero temporary files. More workers improve queue throughput; these observations do not establish a fixed speedup or guarantee full CPU utilization when YouTube/network waits dominate.

Synthetic/local integration checks cover simultaneous claims, one execution per batch, real overlapping execution in spawned processes, combined retained-cache reservations, exact pixels and original PTS after remuxing a nonzero-origin section, cancellation that kills the acquisition subprocess, stale-directory cleanup, and pool shutdown cleanup. Private measurements are under `data/parallel-worker-live-validation.json` and `data/adaptive-video-cache-performance-validation.json`. After the faster queue reached the old 2-GiB retained-cache cap, all executions stopped cleanly and the local server was restarted as `HOGLINE_MAX_CACHE_MB=8192 uv run hogline serve --workers 8`. Only originally active analyses paused by shared-cache exhaustion were resumed; their individual budgets stayed intact. The 8-GiB retained allowance is separate from disposable working video. See `data/parallel-worker-cache-resume-validation.json`.
