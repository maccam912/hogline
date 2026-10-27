# Pilot annotation guide

The first labels must distinguish a missing observation from a negative observation. Do not turn a short activity sample into a confirmed game boundary.

## Review a recording

1. Check the sheet in the recording itself. The title is a hint. Physical A/B end labels and scoreboard crops remain unverified in `profiles/wausau-v1.json`.
2. Scan short sections of a long source, including likely play, quiet periods, maintenance, and lights-off periods. Record what is unscanned. Sparse sampling can miss short activity.
3. Open the original timestamp link for every proposed transition. Widen the scan and use the original video where the boundary is unclear.
4. Mark each candidate game separately. A recording can hold multiple games. Link another recording to the same canonical game for a continuation, overlap, or alternate view. Do not create a second canonical game merely because another upload shows it.
5. Keep boundaries proposed until independently checked. Use partial coverage if the beginning or end is absent. Completion remains unknown unless there is evidence of the actual conclusion.

## Record scores

- Observed end index and official end number are separate. A fragment's first visible end may be official end 5; leave that number unknown until verified.
- The board has fixed cumulative-point columns and movable **end-number cards**. The number on a card is not the number of points scored.
- Compare a stable pre-cleanup house view with persistent board state. Delayed board updates, stacked cards, overflow, and blank-end conventions need separate confirmation. This build preserves frames but does not yet interpret cards automatically.
- Choose played, blank, unknown, or unplayed explicitly. Unknown/unplayed scores are null. A verified blank is 0–0. Ordinary played ends score for only one color.
- Attach the exact evidence frame and check its YouTube timestamp. A cached crop is a seed view; use the full composite when context matters.
- Confirm the final result independently. Never infer a winner by summing a fragment's visible ends or assuming that stopped play means the leading team won.
- Record your reviewer name and a reason for each revision. Preserve uncertainty and conflicting claims in notes.

## Establish detector evaluation

Before training, freeze held-out recordings grouped by day/game and duplicate-upload family. Include all eight sheets, both physical directions, empty houses, clusters, borders, partly visible stones, people, glare, and darkness. Keep adjacent frames and alternate uploads on the same side of the split.

Label granite footprints separately from colored handles; handle centers are not ground-plane stone centers. Label occluded/unobservable cases explicitly. The current OpenCV output is a baseline for comparison, with no established precision/recall or automatic score-acceptance authority.

Fully annotate one long day, including missed games and non-game intervals, before reporting game-discovery recall. The six retained pilot frames are a source-access/geometry smoke check, not a representative evaluation corpus.
