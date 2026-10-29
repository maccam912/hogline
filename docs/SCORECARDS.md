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

Every new observation pins its original image/PTS, source metadata, parent reader, stone-detector output, crop profile and header references. Checked overhead handle positions accompany each board frame; they are observations, not final stone scores or proof of empty ice. No video is fetched for this retained-frame pass, and no original frame or earlier interpretation is overwritten. Current reader versions replace earlier prototypes only in read projections; prior observations remain stored.

Results uses `scorecard_reconstruction` from the source end-timeline API. JSON exports include checked `scorecard_observations`, their `scorecard_parent_observations`, and reproducible `scorecard_timelines`. Exporting a multi-recording archive groups detailed play input by source so the per-source reconstruction bound does not incorrectly reject the whole archive.

## Development evidence and remaining work

The initial cached-card reconciliation found four January 16 Sheet 5 histories with scores through 7/6/3/5 posted ends, 21 end scores. The new center reader also recovers the explicit blank end 3 in March 17 Sheet 4 footage, yielding complete posted prefixes where the earlier output had gaps. These are development-footage results, not held-out accuracy or proof that a last posted end is the final physical end.

Continuous ice coverage, settling/clearing and direction changes, granite-footprint scoring near final states, delayed-posting association, unposted/conceded final ends and representative full-game labels remain active work. Private measurements and diagnostic images are under `data/whole-recording-score-diagnostics/`.

Supported FFmpeg/FFprobe builds use persistent bounded HTTP byte-range requests after a capability probe. Unsupported builds retain compatible reconnect options. Disposable video sections still preserve original timestamps and clean up on success, failure and cancellation. One real eighteen-minute 1080p acquisition took 23.37 seconds; this is a development timing, not an archive-wide speed guarantee.
