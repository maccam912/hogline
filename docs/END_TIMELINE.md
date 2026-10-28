# Reconstructing games, ends and scores

Choose **Reconstruct ends** on a recording. The workbench builds a source-linked ledger from cached board and detailed-window evidence. **Analyze cached houses** adds experimental granite-edge score proposals under a 24-frame / 30-second budget; repeat to continue the backlog. No video is fetched by either action. Open the JSON links for the complete measurements and input references.

This is a first complete-game reconstruction milestone, not a validated complete-game extractor. It deliberately exposes missing ends, uncertain boundaries and score disagreements instead of producing a complete-looking scorecard.

## Independent clocks and evidence

The output keeps these distinct:

- A first visible house-handle appearance bracket. This may be an arrival, placement or detection recovery; it is not automatically the first delivered stone. Guards and hogged stones may never enter this view.
- Sampled configurations that settle, and possible clearings. A pre-clearing stable configuration may precede an unobserved final stone. Neither determines when players agreed the score.
- The later interval in which a numbered card becomes repeatedly readable. A card can be posted late, hidden, moved or corrected. This interval never substitutes for end completion, and a measured posting lag remains null until play timing is established.

House-focused play cues examine a fitted outer-house ellipse plus a 16% margin. This excludes some parked stones and border objects while retaining inside-region rejected candidates and obstructions. Four distinct composite samples spanning at least 1.5 seconds are required for stability. Existing ten-second transition checks remain available. Additional occupied/zero-detection and first-handle brackets may span up to 180 seconds **only with contiguous dense sample coverage**, matching acquisition/crop/detector/visibility inputs, and compatible endpoint geometry. Intervening activity remains explicitly unresolved. A frozen individual camera panel remains a known failure mode.

Overlapping windows do not supply independent evidence or extra physical shots. Camera views are not fused into counted deliveries. Exact original PTS/timebase, decoded frame hashes, parent window/result references and source links are retained. First throw, last stone stopping and score agreement remain null when unsupported.

## Board score histories

Both colored rows must have three shared, distinct readable evidence frames spanning at least two seconds. Separate nearby red/yellow readings cannot fabricate a simultaneous board state. Data from different source manifests or reader versions stay separate.

The ledger keeps official end numbers independently of detected activity. For one color, differences between observed cumulative columns propose points scored. A scoring card's end number is not its points value. The first observed cumulative total has no invented preceding zero. Unseen initial totals stay null. A missing numbered card does not establish a blank end. A score after an end is proposed only where its components can be reconstructed; unresolved components stay null.

Card removals or moves split histories as a **reset or correction**, preserving the preceding score history. The segments are not automatically declared separate games. Conflicting end ownership and non-increasing cumulative columns are excluded and exposed in paired epoch issues. Highest observed end number does not establish scheduled ends, ends played, game completion, or a final winner. A saved game's confirmed consecutive end records and explicitly complete coverage/completion can provide an ends-played count; scheduled ends remain unknown.

## Experimental house scoring

The edge estimator searches near each colored handle for a supported circular granite perimeter that extends beyond the colored component, with edge support around all four quadrants. It retains failed fits. This is an image heuristic: clothing, painted rings/logos and partial stones can still confuse it. It does not replace a trained, independently validated stone-instance model.

The scorer affine-normalizes candidate footprints against the fitted outer house ellipse. It compares uncertainty intervals for the nearest granite edge to the tee and for house eligibility. It abstains on unclear boundaries, rejected color objects, known obstruction, close opposing stones, uncertain extra points and biters. Zero detected handles cannot produce a blank score. Candidate scores are image-space estimates without surveyed/projective calibration or a completeness guarantee. A mid-end house score is not the played result.

A possible clearing can be compared with a later board update within a bounded temporal neighborhood. Numeric vision comparisons require a score from an **exact retained endpoint frame**, matching source manifest, crop profile and view. Multiple possible ends or clearings make the association ambiguous. Agreement/disagreement remains exploratory; the same recording is not independent validation of itself. No automatic score acceptance or game edits occur.

World Curling's [rules](https://worldcurling.org/rules/) and [2025 R12 scoring text](https://worldcurling.org/wp-content/uploads/2025/08/Rules-2025.pdf) describe house eligibility and nearest-stone scoring. The newest PDF linked from the design was unavailable during this implementation; geometric tests cover the ordinary scoring logic, not event-specific remedies or handicaps.

## API, CLI and releases

```sh
uv run hogline end-timeline SOURCE_ID --output /absolute/path/ends.json
uv run hogline end-timeline SOURCE_ID --start 1200 --end 3600
uv run hogline game-end-timeline GAME_ID
uv run hogline score-houses SOURCE_ID --limit 24 --max-seconds 30
```

- `GET /api/sources/SOURCE_ID/end-timeline` accepts optional paired `start`/`end` bounds.
- `GET /api/games/GAME_ID/end-timeline` pins the saved game revision and interprets each source span separately. Source clocks/continuations are not silently joined.
- `POST /api/sources/SOURCE_ID/house-scores` accepts `limit` (1–120) and `max_seconds` (0–120).
- `GET /api/sources/SOURCE_ID/house-scores` and `GET /api/house-scores/ID` expose stored observations.

JSON releases now include `house_score_observations`, `window_play_evidence`, `play_input_exclusions`, and `end_timelines`, derived in the release's existing consistent snapshot. Older dense schemas are explicitly excluded; eligible missing/corrupt dense inputs fail rather than producing a misleading empty report. Cached play derivation is bounded to 128 windows per request. CSV retains its existing reviewed game/end format. Confirmed-mode game/score filters remain intact; experimental observations remain explicitly proposed even in a confirmed-mode archive snapshot.

## October 28 development evidence

A new bounded survey sampled the entire 7,455-second March 17 Sheet 4 recording at approximately one-minute intervals. Eight chunks completed 139 sample requests (overlapping chunk contexts included), with 292,722,734 retained evidence bytes and 557.74 worker seconds. The retained-frame OCR pass processed 103 pending frames. This is additional coverage of a **development recording**, not a new held-out accuracy estimate.

The first assembled report had 158 current-reader frames and four repeated two-row epochs. It proposed red +1 in end 2, red +2 in end 5, and a 5–3 cumulative state after end 5. End 3, initial baselines, play boundaries, end count and final result remained unresolved. The histories and evidence are in ignored `data/full-recording-end-timeline.json`.

Three further 60-second windows sampled source 2520–2700 seconds at 0.5-second cadence without actor inference. The model could bracket a first visible bottom-house handle appearance at **2667.5–2683.5 seconds**, but could not establish a valid final pre-cleanup state in the preceding top house: rejected candidates and obstruction prevented it. The first throw and official end association remain unknown. This is a retained development failure/partial observation, not successful automatic end extraction.

A visual check of the initial granite prototype caught fits to colored handles rather than granite. Requiring an edge beyond the colored component tightened the method; earlier experimental versions remain in the catalog. The tightened scorer was then run on 120 development frames (240 house views): only two views received provisional numeric scores; most abstained for no handles, uncertain granite boundaries or obstruction. Seventy-four source frames remained pending. These are output/abstention counts, not correct-score counts. No independently reviewed end-score or complete-game accuracy rate has been established. Next validation needs original-interval labels for actual first releases, final stones settling, pre-cleanup granite footprints, board updates, and complete game boundaries on untouched recordings/days.
