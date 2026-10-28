# Bounded automatic analysis

**Run automatic analysis** connects bounded stages under one durable processing batch:

1. Coarse activity scans acquire sparse frames at up to 640 × 360. Ten-minute chunks are admitted round-robin across selected recordings, up to the requested new-scan limit. Completed matching scans are reused.
2. The activity selector chooses short detailed windows, reserving 20% of slots for quiet audits when eligible quiet samples exist. It balances sources and records supporting observations, selection reasons, source timestamps, and YouTube links. Existing compatible completed or in-progress windows are excluded.
3. Supported delivery/control brackets can trigger up to two four-second refinements at native-frame cadence (up to 30 targets/second), before OCR. Competing camera brackets are deduplicated by overlapping source intervals. Missing hand control alone does not trigger acquisition. Set `delivery_refinement_jobs` to 0 to disable this stage or at most 8 to raise its job cap. Selection is finite and recorded with the parent result checksum; refinements do not recursively select more refinements.
4. Pending original-resolution retained frames in the selected source/time range receive local scoreboard and clock OCR. Frames are selected round-robin across recordings, then chronologically. Coarse images and evidence from older metadata snapshots are excluded. Each selected frame is a separate durable work unit; successfully read frames survive a later interruption.

The exact detail and OCR selections depend on earlier outputs. The initial preview states their limits and shows the concrete initial scan intervals. Each later selection is recorded atomically in the batch's phase history before its work runs. A bounded campaign can also use compatible evidence previously acquired in the same requested range.

All stages share the **same cumulative retained-data growth and worker-time budgets**. Starting a later stage does not refill either allowance. Pause, resume with larger limits, cancel, failures, retries, lease expiry, and execution-cost history use the existing processing controls. Restarting the local server or CLI worker continues the current stage. A completed campaign only means its admitted work finished: deferred scan chunks, unselected windows, and unread frames remain explicitly reported, and sparse sampling is not full recording coverage.

No games, end scores, final results, or identities are accepted automatically. OCR readings and house observations remain proposals. No full recording is downloaded or retained. Media access still uses bounded seeks/short decodes; transfer bytes are not currently measured or capped independently. Worker time is checked cooperatively between operations, so an in-flight decode or metadata resolution can overshoot a time limit before the batch pauses. Retained-data reservations happen before writing new assets.

## CLI and API

Save an `AutomaticRequest` JSON document locally, for example:

```json
{
  "source_ids": ["an-imported-source-id"],
  "start_seconds": 600,
  "end_seconds": 1200,
  "scan_jobs": 2,
  "scan_interval_seconds": 30,
  "detail_jobs": 4,
  "delivery_refinement_jobs": 2,
  "window_seconds": 10,
  "ocr_frames": 24,
  "max_image_growth_mb": 128,
  "max_worker_seconds": 600
}
```

```sh
uv run hogline automatic-plan data/campaign.json
uv run hogline automatic-plan data/campaign.json --enqueue
uv run hogline work
```

The UI uses `POST /api/automatic/preview`, then `POST /api/automatic/batches` with the original request and `expected_plan_sha256`. A changed source or matching scan plan rejects a stale preview. The phase selector pins source metadata, layout, and processing methods. If those change mid-campaign, it pauses for inspection instead of silently changing the plan's meaning. The OCR phase additionally pins its selected evidence manifests and retained header-reference version.

Source selection is capped at 16, new scan/detail jobs at 32 each, delivery refinements at 8 jobs, and OCR at 120 frames. No recurring campaign is scheduled implicitly. Start another bounded campaign to continue a backlog.

## Validation

The synthetic eight-sheet integration run exercises scan, detail and OCR stages, quiet-audit selection, per-source admission, original PTS/timestamp links, proposed-only outputs, and restart between phases. Failure tests cover pause/cancel, cumulative budget exhaustion at a phase boundary, partial OCR recovery, stale previews, source/method drift, and empty/reused stages. These verify orchestration contracts, not vision accuracy.

A real two-source run on the March 17 Sheet 2 and Sheet 6 recordings inspected source time 600–660 seconds: four coarse frames, two ten-second detailed windows, and five retained OCR frames. All nine units completed in 46.52 measured worker seconds with 9.96 MiB reserved against a 180-second/64-MiB shared allowance. The selector found no eligible quiet samples in that small range and disclosed zero quiet audits; two other candidate windows remained deferred. This is a bounded integration check across different layouts, not a game-recall or OCR-accuracy measurement.

A separate synthetic campaign test exercises automatic insertion of one native-cadence refinement between detail and OCR, overlap deduplication, unchanged cumulative budgets, and finite completion. Brackets must belong to admitted parent windows and current source/actor lineage. At most 32 parent windows and eight prioritized brackets per result are considered. Existing failed/running matching refinements are left for attention rather than duplicated. These tests establish scheduling and lineage behavior, not real delivery precision.
