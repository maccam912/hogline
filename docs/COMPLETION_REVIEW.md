# Near-complete game review — October 30, 2026

Bulk processing is paused. The live archive has 136 paused recording analyses, no active batches and no running jobs. Successful measurements and queued work remain saved; bulk processing has not resumed.

Three games were reviewed from newly decoded native 1920×1080 source frames, including original presentation timestamps. Independent visual reviews are stored as checksummed `assistant_visual` proposals. Original completed analysis reports remain immutable; the Results tab reconstructs the current scores with these additional reviews.

| Recording | Earlier result | Reviewed result, red–yellow | Missing input |
| --- | --- | --- | --- |
| [Sheet 4, March 14](https://www.youtube.com/watch?v=3XVH1aLI3uk&t=7270s) | Eight posted end scores; final result unresolved | **6–5 after eight scored ends** | One yellow stone wins end 8. Handshakes/clearing and subsequent medals establish a proposed game ending. A counter reading 9 and an additional isolated delivery after handshakes do not alone establish another competitive end. |
| [Sheet 5, February 28](https://www.youtube.com/watch?v=LdnFvXbLYLA&t=8040s) | Eight posted end scores; final result unresolved | **7–6 after eight scored ends** | Final ice shows red +2, followed by handshakes and awards. End 8 is posted much later than play finishes. The reader had scores but no independent game-ending review. |
| [Sheet 4, February 28, first game](https://www.youtube.com/watch?v=HMCPzJjUpmc&t=9435s) | Scores through end 7, 7–7 | **9–7 after eight scored ends** | Final ice shows red +2, then handshakes, clearing and ice preparation. No eighth-end posting was recovered before the board reset. A later game in the same recording still has only four recovered end scores. |

The review IDs are `86f42826-0b94-5a94-a88b-5ba45c6b784b`, `0b1a545b-900b-5115-8c9a-b2c58f4a2cad`, and `a117d29e-6524-56a3-9197-8a02e44e86e1`, respectively. Native final-house and phase frames are available through each review's evidence links in Results. Local capture metadata, review requests, saved reviews and reconstruction reports are under ignored `data/completion-review/`.

## What prevented completion

The automatic pipeline reads cumulative score cards and creates score histories, but it does not generate the independent game-ending visual review required by score reconciliation. Completing all processing stages therefore does not imply a finished game. One of these samples also needs an ice result because its final end was not recovered from the board.

Games can be scheduled for six, eight or ten ends, and concessions or extra ends can change the actual number played. These three reviews establish eight scored ends from their ending sequences. They do not establish the scheduled format. No fixed end count is used to accept a final result.

Routine retention had released every original context image for these older recordings. The timestamped frames were reacquired for this review. Independent ice-review evidence was also missing from the retention protection list; that is now fixed so saved decisions remain visually inspectable.

## Changes and remaining extraction work

- Results distinguishes recovered scores, partial score claims, no recovered scores and complete proposed results. It lists per-game missing scores, competing board readings and missing ending reviews, with evidence links and review notes.
- New analysis summaries count the temporal/reconciled score histories displayed in Results. Previously they counted only the older simultaneous-row ledgers, which could report zero candidates despite recovered end scores.
- Saving a visual review and fetching its scores now load board observations only for that recording. The prior archive-wide fallback loaded unrelated recordings and made a single review unnecessarily expensive.
- Independent review evidence is protected from routine image eviction. Historical analysis reports and earlier observations are preserved.

The next automatic extraction improvement should select short, dense windows around each candidate game's final board changes and subsequent house clearing, then reconcile that ice evidence with delayed or absent board postings. This must cover internal game boundaries, not just the recording's tail. Current-end and blank-plate inspection is a separate tool and was absent from these automatic runs; integrating it would provide better pending-end diagnostics, without making a current-end counter proof of completion. End count, a reset, an empty house or an isolated extra delivery must not individually trigger a final result.

These are three development-footage reviews, not a held-out accuracy measurement or proof that remaining recordings reconstruct completely.
