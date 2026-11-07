# Recording and batch workflows

The recording workspace puts common tasks above the results. Date-range, weekday and sheet filters are in the recording list, including on small screens.

- **Review decisions:** inspect one saved game boundary or end score. Adjust the values, confirm or dispute, and save a revision. Each decision has its own evidence; confirming boundaries does not confirm scores or a final result. **Review / adjust score** beside a proposed end first asks which saved game it belongs to.
- **Players & results:** choose a saved game to compare participants’ measured weight, line error and sweeping. Missing measurements remain unavailable. Face names and shot participants are separate until supported by reviewed associations. Use the game-specific shot-analysis link to add observations.
- **Find / redo faces:** opens only this recording’s lineups. **Redo face scan** deletes that game’s face groups and name links and queues fresh samples in one database transaction. Names remain available to assign again. **Adjust grouping** reuses existing face images.
- **Recording actions → Clear analysis / start again:** preview the automatic results to remove. Clear alone or clear and queue another analysis. Original media, cached evidence, saved games, independent reviews and face data remain. This is not a disk cleanup. Active work and dependent saved annotations block clearing; the dialog explains why.

## Date-range batches

Choose **Batch add & analyze**, inclusive dates, sheets and any combination of weekdays. Optionally enter **Video title contains**, for example `Koski`, to add, download and process only videos containing that text in their titles (case-insensitive, literal substring matching). Leading and trailing spaces are ignored; leave it blank for all titles. This combines with the date, sheet and weekday filters. Choose either the current archive or a YouTube channel/playlist, then preview and start. The channel preset is Curling Stadium Wausau (`@curlingstadiumwausau`). All dates and weekdays are visible by default; no season is privileged.

A saved batch continues without the browser open while the server runs. It advances the channel listing, imports metadata, refreshes matching recording manifests, and queues recording analysis. It discovers game and score proposals within recordings; it does not assume one recording equals one game or automatically confirm results. Existing active, paused and completed analyses are reused. Automatic face finding follows the server’s existing configuration.

Date matching uses titles, not verified in-video clocks. Undated channel entries receive a metadata lookup before filtering. Month/day titles use a publication date to resolve the year only within seven days; dates that remain unresolved and excluded junior-event title words are not analyzed. Channel discovery reads listing pages but only imports titles containing the chosen text. Dates and sheets are checked before analysis, so matching titles outside those limits may still appear in the archive. Concurrent batches on one channel share discovery and import titles matching any of their title filters. Changing the discovery title scope restarts the listing pass so previously skipped matches can be found; existing archive entries are preserved. Preview counts cover already known matches and can grow as discovery proceeds. A listing limit, changed listing anchors or discovery failure requires attention rather than claiming complete coverage.

Limits apply to each recording. Batch progress lists each recording and its error, if any. Pause stops new admissions and pauses analyses created by this batch; an in-flight listing or metadata request can finish. Existing analyses reused by the batch are not controlled by it. Resume continues owned paused analyses where their budgets permit. Jobs & progress exposes individual limits and retries. Cancel stops remaining owned analysis work and preserves results already produced.

The clear/rescan controls are destructive only when explicitly used. Developing and testing these screens does not clear recordings, confirm reviews, or start an archive batch.

Current-channel titles such as `Oct 30 — Friday 6:15 - Mixed League — Koski, Lindsey vs Dobbs, Jason (Sheet 4)` supply the sheet, draw description and both team names. Existing imports receive these derived hints when displayed, without changing their evidence manifests. Names appear in the recording workspace and as team suggestions in the game and lineup editors. Title order never assigns red/yellow, identifies individual faces, or establishes a roster.


### Ended streams still processing on YouTube

Analyze recording can first download an ended (`post_live`) stream, verify it, and analyze
that local copy. Live/upcoming streams remain blocked. The fallback fetches video only at
up to 720p using yt-dlp, refuses missing fragments, and checks duration, start time, and
seek timestamps near the beginning, middle, and end. A discrepancy of up to five seconds
is recorded; analysis stops at the shorter verified duration. This is a coverage check,
not proof that every frame is intact or that the game itself is complete.

The file is retained under `data/post-live` for reuse, with its SHA-256 recorded in media
provenance. It is separate from the evidence-image budget. yt-dlp uses a 4 GiB download
limit and a two-hour command timeout. Command-line preparation is also available:
`uv run hogline prepare-recording SOURCE_ID`. Date-range batches can use already prepared
copies; they do not yet initiate this download fallback themselves.

### Recording follow-up stages

The recording workspace shows separate score, face and skill stages, including missing
models, unreadable scoreboard frames and paused skill campaigns. Completion of the score
pipeline does not imply that face scans or skill surveys have finished.

After recording analysis, automatic skills creates an **unconfirmed** game workspace only
when the title contains one matchup and no multiple-game score history is present. Existing
games are reused. Surveys cover each game span in chunks of at most 30 minutes; supported
delivery proposals feed bounded local motion batches. Repeated scheduling reuses existing
campaigns. Surveys require the existing vision configuration and obey its spending budget;
`HOGLINE_AUTO_SKILLS=0` disables automatic scheduling. Missing settings and exhausted
budgets are visible in the workspace. Generated evidence is provisional: confirmed skill
ratings still require reviewed player links, calls and camera calibration.

Pretrained face, person and pose models now default to `hogline-assets/models/` beside the
resettable data directory. `HOGLINE_MODELS_DIR` overrides this location, and existing
per-model overrides remain supported. Legacy `data/models/` files remain usable when no
persistent copy exists. Learned models trained from this catalog remain in `data/`.
