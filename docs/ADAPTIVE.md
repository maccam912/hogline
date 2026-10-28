# Activity-guided detailed windows

**Choose detail windows automatically** uses retained sparse observations to select a bounded set of short detailed intervals. It does not download or scan entire recordings, and previewing acquires no media. Starting the preview creates a durable batch using the same pause/resume/cancel, cumulative worker-time, retained-growth, and attempt accounting as recording plans.

The selection policy `activity-guided-window-selection-v1`:

1. Uses only owned samples with available evidence records, the current activity method, matching camera panels, and the current source manifest. Changes confined to scoreboard templates do not invalidate camera-panel motion. Earlier methods, changed crops, missing samples, and stale source scans are counted as exclusions.
2. Centers a 4–60-second window on each eligible sample, clamped to the requested range and recording duration. Default detail spacing is 0.5 seconds. Sparse quiet/activity changes in contiguous scan ownership receive transition priority; they are not asserted game or end boundaries. Both supporting observations are pinned in the selection record.
3. Reserves a requested share (default 20%, rounded up to a whole window) for quiet-sample audits. If there are insufficient quiet candidates, the preview reports fewer audits. The remaining capacity favors activity transitions, motion, then uncertain/dark samples. Recordings with fewer selected windows go first, and a persisted seed supplies deterministic tie-breaking. This balances recordings, not a guarantee of one window per sheet.
4. Avoids overlaps among newly selected windows and skips fully covering queued/running/completed work with matching source, layout, detector, decoder, persistence version, and adequate sample spacing. Partial overlap with historical work may still be needed to observe a complete new interval. Failed or exhausted matching work is reported for attention rather than silently retried.
5. Limits each preview to 16 recordings, 10,000 unique sparse sample times, and 64 admitted windows. Other candidates remain deferred, including those overlapping admitted work. Unscanned gaps remain unknown and require a coarse recording plan first.

The preview hash is rechecked inside the batch-publication transaction. A changed input or matching job invalidates the preview instead of enqueueing a different selection. Selection records retain source hashes, scan hashes, supporting frame IDs/times/states, reason, and a YouTube link. Batch execution follows admitted unit order, so shared-budget limits do not let random job IDs replace selection priorities. Actual limits can pause the batch before all selected windows complete. Network bytes are not measured by these retained-data limits.

## CLI / API

```sh
uv run hogline adaptive-plan /absolute/path/adaptive-request.json
uv run hogline adaptive-plan /absolute/path/adaptive-request.json --enqueue
```

A minimal request is `{"source_ids": ["SOURCE_ID"], "max_jobs": 4}`. Optional fields include `start_seconds`, `end_seconds`, `window_seconds`, `interval_seconds`, `quiet_audit_fraction`, `selection_seed`, `max_image_growth_mb`, `max_worker_seconds`, and per-job allowances. HTTP routes are `POST /api/adaptive/preview` and `POST /api/adaptive/batches`; the latter takes `request` and `expected_plan_sha256`. Use the existing batch-control route for pause, resume, or cancellation. **Preview remaining** skips completed covering work without overwriting the earlier batch.

## Pilot and limits

A browser-started real batch selected four ten-second intervals from Sheets 2, 4 (two recordings), and 5: one quiet audit, one activity transition, and two motion samples. It produced 160 columnar rows in 36.8 worker seconds with 19.2 MiB retained growth under 64 MiB / 300-second limits. All artifact hashes and row counts checked. Previewing the remainder excluded all four completed windows. Synthetic tests also exercise all eight sheets, source balancing, stable selection, overlap suppression, stale previews, profile changes, and pause/resume.

One automatically selected Sheet 4 interval exposed a **false persistent configuration**: three real visible handles plus a red garment patch on a stationary gray-clothed person. The dark-foreground check missed the person. Both retained anchors are now assistant-labeled development cases; v5 has 45 matches and 2 false positives across the enlarged 19-view corpus. The prior frozen 17-view result remains unchanged. This illustrates why targeted detector improvements are not release accuracy and why the selector never confirms a game state or score. Stronger foreground visibility checks and independent evaluation remain necessary.
