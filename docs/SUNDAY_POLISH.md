# Sunday review batches

The current scope is Sunday games in the **2024–25 curling season**. The user corrected the season on October 30: they took 2025–26 off. The user's team label is **Koski Team**. Draws varied, usually in the afternoon; 4 p.m. is a useful starting point, not a hard filter.

## Preview, select, then inspect

Open `/screening` from **Find my team**. The initial corrected gallery contained 32 single-frame previews across January 26, February 2, February 16 and February 23, 2025, covering sheets 1–8. Selection targets 4:30 p.m. using stream-start metadata; these are approximate clock times, not verified overlay readings. Recording titles supply dates. Each source received metadata import and a single preview capture, without full analysis. The previous 2026 gallery is preserved in `data/screening/manifest-2025-26.json` and is no longer presented for team selection. The gallery is a sample, not complete season coverage.

The operator marks **My team is visible**, **Not visible here**, or **Unsure**, with optional notes. Labels apply only to the displayed moment. An absent team in one image does not exclude the recording. Labels are revisioned, tied to the preview hash, and preserved in source history without changing source metadata identity. Replaced previews need fresh labels; stale browser submissions conflict. No analysis jobs are launched by labeling. This is manual visual selection, without automated facial identification or matching across streams.

For the next review, use the saved positive labels to choose up to four games, including any overlapping recordings needed to cover their endings. Reuse completed analysis and retained observations first. Acquire only missing evidence. Wait for all selected work to finish, inspect the resulting scores and completion against original footage, fix general errors with regressions, and check the fix on a different four-game group. Do not resume the archive-wide backlog or infer team identity from a player's face.

## Historical validation group: March 8, 2026

This group is outside the corrected playing season and is retained only as validation of the reconstruction fixes. All four existing pipelines had already completed. Their measurements were reused, and targeted native frames were reacquired for review. These are assistant visual proposals, not human-confirmed results.

| Sheet | Posted end scores | Last unposted end | Proposed final, red–yellow | Scored ends |
| --- | --- | --- | --- | --- |
| 3 | Y2, Y2, Y1, Y1 | R1 | 1–6 | 5 |
| 4 | Y3, R2, R1, R1, Y1 | Y1 | 4–5 | 6 |
| 5 | Y4, R1, R3, Y1, R1 | R2 | 7–5 | 6 |
| 6 | R3, R3, R1 | R2 | 9–0 | 4 |

The review checked repeated scoreboard states, final deliveries, stable scoring stones before clearing, and handshake/packing/reset sequences. Stones pushed through the house during cleanup were excluded. It did not audit every delivery, individual release timing, or the scheduled game length. Twenty-one end scores and four proposed complete results are available through the source end-timeline APIs. No team membership was assigned.

## Fixes and forward evidence

Temporal scorecards v3 rejects weak competing end labels in the same team's cumulative scoring column. Sheet 5's end-3 card was occasionally read as end 8; 44 readings supported 3 and four supported 8. Rejected alternatives retain their provenance. Comparable support remains unresolved, and blanks are exempt. Immutable older reports can still reconstruct with v2.

Four different March 1 recordings (sheets 1, 6, 7 and 8) were checked from retained observations and one original board frame each. Their five posted scores per game matched the boards. The fix also removed an erroneous end 7 on Sheet 7 that was a misreading of end 1. These are score regression checks, not four additional complete-ending reviews.

New recording plans prioritize bounded detail windows before observed board resets and recording tails. They use the existing window budget, preserve selection provenance, and do not treat a reset as proof of completion. Old paused plans retain their pinned strategy. The strategy has planning and synthetic pipeline tests and has now completed three selected 2025 recordings; their final results still require the separate visual review described below. Result summaries also no longer count a finished game with missing/conflicting scores as complete.

## Local checkpoint and evidence

Initially, 133 never-started analyses were cancelled and three partial analyses were left paused. The subsequent user-authorized cleanup below supersedes that retention checkpoint. The main list defaults to Sundays in 2024–25 (by recording title date), with an explicit season selector. The filter uses July 1 through June 30 to span both calendar years; it is an archive filter, not a claim about official league dates. Completed recordings remain available; unfinished, unselected sources were removed by the subsequent cleanup.

Private evidence is under `data/sunday-polish/`: `batch-one-results.json`, per-source `review-request.json`/`saved-review.json`/`after.json`, captured images and exact source timing, `forward-check.json`, `queue-before.json`, `queue-change.json`, and `final-validation.json`. The gallery's content-addressed JPEGs and manifest are under `data/screening/`. One-off selection/capture helpers remain alongside the private evidence; gallery generation is not yet a product action.

The full suite passed 482 tests before the last label-provenance safeguard. All 12 affected screening, recording-planning and ending-window tests passed afterward, including the new changed-preview regression. Ruff, formatting, JavaScript syntax and diff checks passed. Browser checks verified loaded images, date filtering, the Sunday-only inventory and no preview-page script errors. Existing unrelated working-tree changes remain uncommitted.

The season correction imports 40 Sunday source records, including eight unused March 2 candidates and captures previews only. Current selection/import/capture receipts are in `data/season-2024-25/`; the user subsequently selected three games, all of which completed analysis. February 16 was not positively identified, and a possible bye remains unverified.


## Selected games and cleanup, October 30, 2026

The user selected three 2024–25 games. All three recording pipelines finished, then their native scoreboard and ending sequences were independently inspected. Assistant visual reviews now propose complete results for 17 scored ends, with no remaining score or ending blockers:

| Date | Sheet | Posted scores | Unposted final end | Proposed red–yellow final | Ends |
| --- | --- | --- | --- | --- | --- |
| January 26, 2025 | 8 | Y2, Y1, R1, R1 | Y1 | 2–4 | 5 |
| February 2, 2025 | 3 | Y3, R3, Y3, Y1, Y1 | R1 | 4–8 | 6 |
| February 23, 2025 | 2 | R2, Y1, Y1, R3, Y1 | Y1 | 5–4 | 6 |

These are assistant proposals, not human-confirmed results. Koski Team's stone color and scheduled format remain unknown. Every shot and release time was not audited. Full-resolution evidence matters: February 2's six-oclock red is closer than the northwest yellow (roughly 152 versus 168 pixels from the tee in the near-circular overhead view). February 23's end-5/end-7 OCR conflict was resolved by genuine additional ending-frame observations using the existing reader and threshold; no synthetic observations or case-specific threshold changes were introduced. Immutable review requests and results are under `data/koski-review/{source_id}/`; the summary is `review-results.json`.

For February 16, two additional moments per sheet were captured for manual review. The user rejected sheets 4, 5 and 6. No positive selection was made; a bye is possible but unconfirmed. Supplementary previews are source-scoped, and their hashes cannot be submitted as labels for the original preview. This path has focused API tests.

The user then authorized deleting unfinished database material outside their selected games. Completion was defined as a completed recording pipeline, not independently verified gameplay. After a recovery backup, the cleanup removed 174 sources, 6,262 jobs, 772 batches and dependent incomplete observations. All 525 completed reports retained their original hashes, including the three selected sources. Twenty-four cancelled historical batches and referenced job history remain as dependencies of retained results. Existing selected-source follow-up work was preserved. No archive-wide reanalysis was launched.

The database now has 525 sources; the active screening gallery shows only the three positively labeled games. `cleanup-plan.json`, `cleanup-result.json` and `cleanup-validation.json` record the operation. The pre-cleanup database is in `data/koski-review/cleanup-recovery.sqlite3.gz`; media files were not purged. Restoration requires stopping the server and replacing the database from this backup, and would discard subsequent database changes. The server receipt in `data/koski-review/server.json` is current. SQLite integrity and foreign-key checks passed, all three preview images served successfully, and pending jobs were limited to selected sources. Thirty focused screening, ending-window, result-review and scorecard tests passed, with Ruff, JavaScript syntax and diff checks also passing. No commit or push was made.
