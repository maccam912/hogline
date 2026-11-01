# Ice estimates and authoritative posted scores

Scoring previously stopped when numbered board cards stopped appearing. Recording
processing could finish with only a posted prefix; native ice reviews were a
separate path, and board/ice conflicts could suppress the running total.

Combined scores v5 and grouped scores v4 use a reliably reconstructed end-card
score in preference to an ice score. Original ice claims, review authors, evidence,
alternatives and policy applications remain available. Re-reading a board with
conflicting OCR still requires resolving that reading; an arbitrary OCR detection
is not an authoritative board score. Earlier combined v1–v4 and grouped v1–v3
projections remain reproducible for existing exports.

Completed-end ice reviews extend the history beyond the last posted card. Close
counts retain discrete `score_options`, such as red 2 or red 3, and per-color
`points_range` bounds. `score_after_end_range` carries those alternatives forward;
`final_result_range` and `possible_outcomes` describe a completed game's result.
A missing end invalidates later cumulative bounds; it is never silently zero.
An explicitly unscored concession preserves the previous total. Board corrections
collapse the affected alternatives and recompute subsequent totals. Lower-bound
numeric estimates remain for existing consumers, alongside the ranges.

A singleton ice estimate remains labeled as an estimate. Confidence categories
are scoreboard, ice estimate, uncertain, or unknown; they are not measured
probabilities. Assistant reviews do not become human confirmations.

The house scorers now retain conservative score alternatives when granite-edge
measurement intervals overlap. Occlusion, missing bodies, failed calibration or
unresolved color candidates still produce unknown scores, rather than treating
handle counts as points. A lying house prediction requires end-phase and end
association evidence before it can become an end score. Automatic identification
of every final resting state in arbitrary recordings remains unfinished; this
change does not assert that a stable mid-end house is a completed end.

## October 30, 2026, Sheet 4

The current source is `1c54b78f-e33a-4857-81da-a421c9651f54` (YouTube
`4NHlUXPYEBs`). Repeated board readings recover ends 1–3 as red 1, yellow 1,
red 4: red 5–yellow 1. No later numbered cards were recovered before the board
was cleared. Why the players stopped posting cannot be established from this.

Native frames were reacquired from the verified local download and checked
against the earlier visual review. Three append-only assistant reviews add:

| End | Red | Yellow | Basis |
| --- | --- | --- | --- |
| 4 | 2 or 3 | 0 | Close third red; preserve both alternatives |
| 5 | 3 | 0 | Visual ice estimate before clearing |
| 6 | 0 | 2 | Visual ice estimate, followed by clearing and departure |

Proposed final: **red 10–11, yellow 3**, with end 4 uncertain. All plausible
scores give red the win. The user's six-end report supports the ending review;
no exact first/final delivery time or human-confirmed point count is asserted.

Private evidence, review receipts, the live score history and a pre-review DB
backup are under `data/score-review-oct30/`. Stored completed analysis reports
were preserved. The live recording status now reflects appended reviews, rather
than continuing to show the immutable processing report's old three-end count.
