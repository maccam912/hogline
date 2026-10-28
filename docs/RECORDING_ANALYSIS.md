# Recording analysis and job progress

Select a recording and choose **Analyze recording**. The default scope is the entire recording, rather than the current playhead position or the first ten minutes. A second click opens the existing active/paused analysis instead of creating duplicate work. Imported recordings need known full metadata and duration; recordings up to 24 hours fit this pilot's survey plan.

The workspace starts on **Results**, with candidate game histories and numbered end/score rows. **Video & evidence** contains source frames, the timeline and annotation controls. **Advanced tools** retains the earlier scans, window selection, benchmarks, reconciliation and diagnostic processing tools. Archive-level tools are under **Archive tools**. The selected recording persists across reloads.

The prominent status strip and **Jobs** view show active and paused analyses independently of the selected recording. Completed/cancelled analyses are folded into history. The local worker's online/offline state is explicit; keep the server running for queued work to advance.

## What the main action runs

| Stage | Work |
| --- | --- |
| Survey | Sample the full recording at 60-second intervals, in bounded 18-minute chunks, retaining native detail for boards and overhead observations. |
| Read scoreboards | Read all eligible retained frames for this recording, automatically admitting more pages when the initial 64-frame page finishes. |
| Inspect evidence | Select up to 32 non-overlapping 60-second windows from activity/transitions and quiet audits. Use one-second cadence without body/pose inference for this game/end pass. |
| Read detailed scores | Read additional board evidence retained by those windows, again continuing across pages. |
| Assemble results | Build a checksummed source-linked game/end evidence report. Store the report with the analysis; leave uncertain splits, first/final deliveries and scores unresolved. |

The entire duration receives a **sparse survey**, with selected moments inspected densely. This is not exhaustive frame-by-frame processing, a guarantee that every game will be reconstructed, or permission to accept uncertain results. Candidate board histories can represent resets/corrections as well as games. The Results view labels them for review and keeps unknown scores distinct from zero. The existing [end reconstruction contract](END_TIMELINE.md) still applies.

Survey jobs cover the complete requested duration with no chunks deferred to a separate user launch. Detail selection remains finite and records deferred candidates explicitly. Completed acquisition/reader jobs can be reused. New reconstruction jobs are pinned to the analysis run; starting again after completion creates a new run/report. The worker advances stages automatically and survives server restarts. Missing/corrupt prerequisites, model/source changes, failures and limits pause the analysis visibly rather than silently skipping required work.

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
