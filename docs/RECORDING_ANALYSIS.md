# Recording analysis and job progress

The [October 30 near-complete game review](COMPLETION_REVIEW.md) records three inspected endings, their recovered results, and remaining automatic extraction gaps. Processing completion and game completion are separate: Results now reports recovered score counts and explains missing scores or ending reviews. Games can be six, eight or ten ends; no fixed end count establishes completion.

Select a recording and choose **Analyze recording**. The default scope is the entire recording, rather than the current playhead position or the first ten minutes. A second click opens the existing active/paused analysis instead of creating duplicate work. Imported recordings need known full metadata and duration; recordings up to 24 hours fit this pilot's survey plan.

The workspace starts on **Results**, with candidate game histories and numbered end/score rows. **Video & evidence** contains source frames, the timeline and annotation controls. **Advanced tools** retains the earlier scans, window selection, benchmarks, reconciliation and diagnostic processing tools. Archive-level tools are under **Archive tools**. The selected recording persists across reloads.

The prominent status strip and **Jobs** view show active and paused analyses independently of the selected recording. Finished, cancelled and failed analyses are folded into history. Failed recording analyses show **Needs attention** and retain their errors; they do not block the remaining recordings. The local worker's online/offline state is explicit; keep the server running for queued work to advance.

## What the main action runs

| Stage | Work |
| --- | --- |
| Survey | Sample the full recording at 60-second intervals, in bounded 18-minute chunks, retaining native detail for boards and overhead observations. |
| Read scoreboards | Read all eligible retained frames for this recording, automatically admitting more pages when the initial 64-frame page finishes. |
| Inspect evidence | Select up to 32 non-overlapping 60-second windows from activity/transitions and quiet audits. Use one-second cadence without body/pose inference for this game/end pass. |
| Read detailed scores | Read additional board evidence retained by those windows, again continuing across pages. |
| Assemble results | Build a checksummed source-linked game/end evidence report. Store the report with the analysis; leave uncertain splits, first/final deliveries and scores unresolved. |

The entire duration receives a **sparse survey**, with selected moments inspected densely. This is not exhaustive frame-by-frame processing, a guarantee that every game will be reconstructed, or permission to accept uncertain results. Candidate board histories can represent resets/corrections as well as games. The Results view labels them for review and keeps unknown scores distinct from zero. The existing [end reconstruction contract](END_TIMELINE.md) still applies.

Survey jobs cover the complete requested duration with no chunks deferred to a separate user launch. Detail selection remains finite and records deferred candidates explicitly. Completed acquisition/reader jobs can be reused. New reconstruction jobs are pinned to the analysis run; starting again after completion creates a new run/report. The worker advances stages automatically and survives server restarts. Transient remote-video failures retry automatically within the three-attempt limit, using 5- then 20-second delays and fresh stream URLs. Cached sparse frames are reused; incomplete detailed windows restart without publishing partial results. For recording analyses, a timed-out detailed window gets one replacement attempt with twice its original task allowance (at most 600 seconds). A recording admits at most three such replacements. Original requests, failed attempts and replacement links remain recorded; the recording time/data limits are not raised. If recovery fails, prerequisites change, retries exhaust or limits are reached, the analysis becomes **Needs attention** (`failed`), cancels its remaining work and releases its slot after running siblings stop. The queue continues with the next recording. This failed run is never counted as completed; a fresh analysis can be started later. Other bounded campaigns retain their pause/resume behavior.

## Progress, time and controls

The recording card and Jobs show the current stage, its completed/scheduled tasks, progress, elapsed processing time, retained data and current task/frame progress. For recording analyses, overall progress weights five stages equally; it is not an ETA or an accuracy percentage. Scoreboard page admission uses the original pending-frame denominator to avoid showing the first page as the whole stage. Earlier bounded campaigns show current-stage progress and are labeled separately.

Elapsed processing uses durable execution accounting, including failed attempts. The live timer adds the interval since the current execution's latest heartbeat and freezes when processing stops. Idle and paused waiting do not accumulate as processing time. Each task exposes its current attempt's time, status and attempt count. Database accounting remains authoritative after reload/restart; no browser timer changes budgets.

Use **Pause**, **Retry & resume**, **Adjust limits**, or **Cancel remaining**. Retry/resume is one transaction: if any failed task has exhausted its three attempts or a budget is insufficient, none of the failed jobs is partially resumed. Existing successful work and execution history remain intact. An already running decoder can take until its next budget/cancellation check to stop.

Default additional-data allowance is approximately `ceil(survey_frames × 2.5 + detail_windows × 24)` MiB, bounded to 128–4096 MiB. Processing allowance is approximately `ceil(survey_frames × 8 + detail_windows × 90)` seconds, bounded to 600–86400 seconds. Detail windows default to three per half-hour, bounded to 4–32. These are finite allowances, not predicted costs. **Analysis settings** overrides the next run's time/data limits; changing a current run uses Jobs. The shared retained-data cache limit still applies independently and can pause acquisition. Current end reconstruction is bounded to 128 detailed windows per source/report.

## API and validation

`POST /api/recordings/SOURCE_ID/analyze` accepts `{}` or optional `max_worker_seconds`, `max_image_growth_mb`, and `detail_windows`. It returns the durable batch and `already_exists`. Full source metadata, method pins, selected windows and shared budgets are preserved.

`GET /api/catalog` adds worker status, execution timing on individual jobs, and `progress_summary` on batches. It omits the potentially large reconstruction document. `GET /api/analyses/ANALYSIS_ID/result` returns the checksummed completed report, or 409 while unavailable. `POST /api/batches/ID/control` accepts the existing pause/resume/cancel action and optional `retry_failed: true` for atomic retry/resume.

Synthetic integration checks cover the full pipeline across restart, multi-page reading beyond 120 frames, metadata changes, whole-duration admission, duplicate clicks, budget pauses, result checksum failures, atomic retry limits and worker-state reporting. These tests establish orchestration and reporting contracts; real game accuracy remains a separate validation task.

## Real workflow check, October 28, 2026

The main button was exercised on the complete 2,210-second Sheet 4 recording `f697454b-f6f7-4a95-9dc5-3a4abce90f77`. Analysis `42c50bfa-bd13-4856-ba75-86c89fc35a2f` finished all five stages: three survey chunks, 62 retained-frame board-reading tasks, six detailed windows and one reconstruction task. It spent 479.21 processing seconds and reserved 122,276,037 evidence bytes under the automatically chosen 836-second / 237-MiB limits. No additional stage launches were needed.

The reader did not establish a complete simultaneous two-row scorecard or verified game boundaries. The Results view preserves useful independent-row proposals instead: red +1 in posted end 2, +2 in end 5 and +1 in end 8; yellow +2 in end 6 and +1 in end 7. These are development-footage observations, not confirmed end results or accuracy measurements. The separate earlier 12-recording campaign remains paused after a decoding failure and is visible in Jobs with its two waiting tasks and 258.75 recorded processing seconds.

Ignored proof artifacts are `data/recording-workspace-real-validation.json`, `data/recording-ui-real-analysis.json`, and `data/recording-ui-real-result.json`. The stored report from the development run predates the independent-row Results enrichment; the current source end-timeline endpoint supplies those row proposals separately. Existing stored reports are not rewritten.

## Bounded image retention, October 29, 2026

New recording analyses use `whole-recording-analysis-v2` and the `bounded-recording-images-v1` storage policy. By default one recording holds the acquisition slot through all five stages, with up to four independent tasks running inside that recording. `serve --workers 8 --recordings 2 --recording-parallelism 8` enables a bounded two-recording trial. Workers prefer the admitted recording with fewer active tasks, and either recording can use spare workers while its peer waits between stages. Only the configured number of recording workspaces is admitted; the remaining recordings wait. The per-recording task cap is applied to existing queued analyses when admitted and recorded in scheduling history. An explicit operator pause keeps its slot until resumed or cancelled. Automatic failures instead finish as **Needs attention**, clean up their temporary images, and admit the next recording. This prevents partial image caches from accumulating across failed recordings.

Each original frame is stored once. The four house/perspective crop descriptors preserve their exact PNG hashes, dimensions and pixel bounds; the server regenerates their checked pixels on demand without writing separate crop files. Existing stored crop files remain readable. New acquisition and window keys distinguish this policy from earlier runs.

After successful reconstruction or terminal recording failure, and only when no workers are running, cleanup removes routine original frames and OCR crop PNGs. It retains three evenly spaced exact context frames and their associated OCR crops per recording, under a **128 MiB archive-wide review-image cache**; older review images can be evicted. Independently labeled evidence, confirmed-game score evidence and pinned board-header references are protected. The shared **2 GiB retained-data limit** still applies to working images and dense columnar observations. Protected images and accumulated observations can still exhaust that limit; it is a safety bound, not a promise that any archive fits.

Cleanup preserves evidence descriptors, original PTS/timebases, YouTube links, observation rows, compressed Parquet observations, derived tracks/house states, execution history and checksummed reconstruction reports. A released image returns HTTP 410 with an original-timestamp fallback; reacquiring a source frame is an explicit operator action. Completed reports remain available through the analysis result endpoint. Routine acquisition manifests are discarded after their recording finishes.


## Unattended workers

A server-side supervisor checks the process pool every five seconds and replaces exited workers. A task that stops renewing its 120-second lease, or runs more than 120 seconds past its declared task allowance despite heartbeats, triggers worker replacement. The old process is stopped before its lease is released; stale tokens cannot publish. Existing three-attempt lease recovery bounds repeated crashes. Each worker has a separate temporary-video directory which is cleared on replacement. Idle workers have no task deadline. The catalog includes `worker_restarts` and any supervisor error; errors are retried on the next supervision tick. These safeguards run while the Hogline server is running; they do not schedule a separate Codex task or start the server after a machine reboot.

Cleanup also removes unpublished image files left by failed or crashed tasks, while preserving all images referenced by retained evidence/observations, live recordings, labels and pinned references. Reports and measurement rows remain available after routine images are released.


## Concurrent recordings and cleanup

`--recordings 1-4` / `HOGLINE_RECORDING_SLOTS` controls the number of admitted recording workspaces (default 1). `--recording-parallelism 1-8` / `HOGLINE_RECORDING_PARALLELISM` controls each recording's task cap (default 4); `--workers` controls the total local process pool. An explicit operator pause occupies one recording slot, while another admitted recording can continue. Existing time/data budgets, retries and failure handling are unchanged.

A completed/failed recording creates a cleanup barrier: no new recording tasks are claimed until currently running tasks drain and cleanup finishes. The second recording keeps its already-acquired evidence, and the next recording replaces only the vacated slot. This short quiescent period protects content-addressed images, unpublished writes and shared byte reservations; cleanup never deletes images while another worker could be reading or acquiring them. The shared cache cap remains independent of the number of workers/workspaces. Concurrent recordings do not require permanent retention of their working images.


The sixteen-worker/four-recording configuration is `serve --workers 16 --recordings 4 --recording-parallelism 8`. Fair sharing usually gives four tasks to each ready recording; a recording can borrow spare workers up to its eight-task cap. A fifth recording stays queued until a slot is released and cleanup completes. The shared retained-data limit and bounded recovery remain unchanged. Four recording workspaces do not imply four permanently retained source videos.


## Repairing and retrying a failed run

After an underlying problem is fixed, explicit operator restart preserves earlier errors and measurements. A run that failed only during reconstruction can use saved observations with no video acquisition. A run that failed earlier reacquires released prerequisites under new work keys; its scoreboard stage reads only the current attempt's frames. The original attempt's history remains available. Original completed reports are preserved, and processing/recovery never confirms game accuracy automatically.
