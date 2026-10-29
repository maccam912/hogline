# Whole-recording score histories

The active implementation reconstructs posted end scores across board resets and separately inspects ice evidence. Full physical game completion remains a separate, unfinished inference. Completing an inspection job does not establish a complete game.

## Confirmed club convention

The operator confirmed these Wausau board meanings on October 28, 2026:

- Hanging plates identify the official end. Fixed center-row labels identify cumulative team points. Red hangs above, yellow below.
- A blank-end plate covers its matching center-row number, either facing forward or reversed.
- The far-left center plate identifies the current end.
- Posting can lag play by several shots into the following end.

The scorer subtracts previous cumulative totals to obtain end points. It never reads a hanging end number as a points value. Missing cards do not automatically supply a blank end. Numbered center plates need repeated sightings. Reversed plates retain an unknown identity until repeated simultaneous plate counts and the uniquely missing intervening end numbers agree with a later current-end reading. A current-end change never forces immediate posting or establishes physical completion.

## Independent card reconstruction

`temporal-scorecards-v2` joins repeated individual card sightings within repeatedly clear-board boundaries. Both rows need not be readable simultaneously. Different source manifests or reader versions never add votes to each other. A source time or original evidence frame counts once; contradictory alternative crops abstain. Three distinct frames spanning at least two seconds support a card. Competing repeated readings and invalid cumulative progression remain unresolved.

The ordinary starting total is explicitly recorded as 0–0; handicaps are not configured. A missing prior end blocks a claimed point delta until its meaning is resolved. A repeated clear board separates candidate histories, while a current-end plate greater than one or a center-row blank plate prevents a delayed/blank first-end score from being treated as a reset.

A complete posted prefix through end N means each end 1…N has a supported score. It does not establish the number of physical ends played, game completion, concession, or a final result. Histories retain left/right censoring, reset evidence, source links, card alternatives and unknown reasons.

## Reading the retained evidence

```sh
uv run hogline read-scorecards SOURCE_ID
```

This creates a bounded durable inspection batch, with up to twelve native frames per task, a five-minute task limit and a shared finite budget. It reuses checked board readings where possible and reads current/blank plates. A source-specific retained header reference supports previously unconfigured frames. A separately pinned normal fixed-header image rejects printed numerals that resemble white hanging plates. Comparing header texture is necessary: a desaturated printed 8 passed an early color-only prototype on development footage.

The v3 reader can verify the unchanged right-hand printed columns against its checked source-specific reference when blank plates cover low-number columns. Extrapolated columns must agree with the source geometry, and both whole-patch and regional texture checks must pass. If people hide the colored bands, that same checked geometry can propose a crop, but the current fixed header still has to match. Obstruction or a different neighboring board causes abstention. A completed prior reader pass remains visible until its replacement finishes; versions never combine votes.

Every new observation pins its original image/PTS, source metadata, parent reader, stone-detector output, crop profile and header references. Checked overhead handle positions accompany each board frame; they are observations, not final stone scores or proof of empty ice. No video is fetched for this retained-frame pass, and no original frame or earlier interpretation is overwritten. Current reader versions replace earlier prototypes only in read projections; prior observations remain stored.

Results uses `scorecard_reconstruction` from the source end-timeline API. JSON exports include checked `scorecard_observations`, their `scorecard_parent_observations`, and reproducible `scorecard_timelines`. Exporting a multi-recording archive groups detailed play input by source so the per-source reconstruction bound does not incorrectly reject the whole archive.

## Development evidence and remaining work

The initial cached-card reconciliation found four January 16 Sheet 5 histories with scores through 7/6/3/5 posted ends, 21 end scores. The new center reader also recovers the explicit blank end 3 in March 17 Sheet 4 footage, yielding complete posted prefixes where the earlier output had gaps. These are development-footage results, not held-out accuracy or proof that a last posted end is the final physical end.

Continuous ice coverage, settling/clearing and direction changes, granite-footprint scoring near final states, delayed-posting association, unposted/conceded final ends and representative full-game labels remain active work. Private measurements and diagnostic images are under `data/whole-recording-score-diagnostics/`.

Supported FFmpeg/FFprobe builds use persistent bounded HTTP byte-range requests after a capability probe. Unsupported builds retain compatible reconnect options. Disposable video sections still preserve original timestamps and clean up on success, failure and cancellation. One real eighteen-minute 1080p acquisition took 23.37 seconds; this is a development timing, not an archive-wide speed guarantee.

## Whole-recording ice coverage

`uv run hogline survey-ice SOURCE_ID` queues ten-minute checkpoints across the whole source, using both overhead houses and a reviewed empty-house reference manifest at `data/scoring/empty-house-references.json`. References pin the acquisition manifest, original image/document hashes, exact crop pixels and geometry. They are private development calibration, not independent test labels. Missing or changed references stop before video acquisition.

The default five-second grid selects the first eligible original keyframe per bucket. `--native --interval 1 --start S --end E` refines a selected interval with non-keyframes. Both modes retain integer original PTS/timebase, source-relative time and decoded RGB hashes. Keyframe sampling can miss short events and is never described as continuous delivery coverage. A ten-minute development survey completed in 13.5 seconds; the durable checkpoint completed in 15.9 seconds. Rates vary with concurrency and source.

The registered-background inspector distinguishes a positive empty-ice match from failure to detect handles. It aligns the checked background, estimates bounded lighting changes, fits changed stone bodies and abstains for unresolved foreground or score measurements. A stable score can occur mid-end, so it is not an official end score. Images around repeated empty matches are retained for finer inspection; at most fourteen full composite anchors per segment are kept, using losslessly compressed working memory. The columnar file keeps every sample's measurements without retaining the full video.

`uv run hogline ice-timeline SOURCE_ID --output REPORT.json` checks artifact bytes, schema, pinned input manifest and original frame identities, then lists sampling gaps, empty-house runs, nonempty score proposals and clearing inspection brackets. A clearing bracket can also be a takeout during an end. Direction changes, final states, posting lag and concessions remain to be reconciled before assigning physical end completion. Decode failures publish no partial segment and use the existing durable transient retry policy.

Independent ice chunks and retained-frame scorecard tasks can run concurrently within one recording (three by default; one to eight through batch controls). Other analysis stages preserve their existing ordered execution. Each execution retains its own lease, cost and retry history, with cumulative shared limits and pause/cancel. Processing clocks add elapsed time for all active workers.

The v2 ice inspector retains changed handle candidates even when granite fitting abstains. Matching static reference marks are excluded. Stable candidate runs and sixty-second receiving-house activity changes provide inspection brackets; clothing, parked stones and sparse sampling can still confuse them. They supply neither end numbers nor point counts. Positive empty runs require distinct physical times spanning at least two requested sample intervals, so overlapping native/keyframe samples cannot inflate support.

## Independent visual end reviews and combined scores

`review-ice-end REVIEW.json` saves an immutable independent review with native final-state and phase frames, original source/evidence hashes, an optional pinned board-history basis, reviewer kind, notes and explicit corrections. Assistant visual reviews always remain proposals. They cannot replace a human decision. Conflicting active reviews or board/ice point claims keep the score disputed and its cumulative total unknown.

A game stopping is separate from a completed scoring end. `end_completed: true` is required before a reviewed lying count can supply an end score. A handshake with completion unresolved preserves `lying_ice_points`, while official points, ends played and final result stay unknown. Mid-end concessions and intentionally parked stones must be checked separately. The current club convention for unposted/conceded final ends is still being clarified.

The v2 combined report preserves the longest known score prefix when an additional final/current end is unresolved. Seven scored ends followed by an unknown eighth still report complete scores through7, with end8 and the final result unknown. Existing v1 exports retain their original projection when checked.

`score-history SOURCE_ID --output REPORT.json` and Results combine repeated board evidence with these independent reviews. JSON releases include `ice_end_reviews` and reproducible `reconciled_score_histories`. Exploratory CSV adds explicitly typed `score_history` / `score_history_end` rows with cumulative scores, independent board/ice values, comparison, review IDs and evidence links. Confirmed CSV continues to contain human-confirmed game interpretations only. Source histories can overlap across continuation recordings; they are not a deduplicated physical game count.

`continuation_score_groups` in JSON releases proposes a shared posted history when nearby same-sheet source clocks agree and at least three consecutive scored ends match, including points for both colors. All shared known scores must agree, histories must touch their recording boundaries, manifests must match, and branches or cycles remain separate. Each grouped end keeps every original source claim and the recording-gap estimate. Grouping does not fill missing footage, determine a final result or change canonical games. The group totals currently use posted board histories; independent ice reviews remain in `reconciled_score_histories`. The March17 Sheet4 continuation pair matches seven ends across a roughly170-second gap; its eight posted end scores count once in this proposed grouping.
