# Automatic board location

`traditional-board-color-bands-v1` finds a red row, a blue fixed-number header, and a yellow row within the upper end-wall portion of the retained left perspective view. It measures their geometry in each image instead of copying Sheet 4's crop coordinates to another sheet. This is a Wausau layout prior, not a general scoreboard detector.

The workbench shows the proposed crop under **Clock & scoreboard observations**, even when no scoring template applies. Multiple similarly plausible candidates remain ambiguous; missing or obstructed bands leave the location unavailable. Point columns, storage markers, blank-end conventions, and card meanings remain unknown. Existing source-scoped OCR templates and temporal score checks remain separate.

## Evidence and storage

Every observation retains the original image hash, evidence/source IDs, source metadata hash, original PTS and rational timebase, exact normalized crop bounds, derived RGB hash, dimensions, method version, and timestamped YouTube link. The HTTP crop is derived from the original image on demand and verifies both hashes. Missing or corrupted originals return an explicit unavailable response. No additional crop image is retained, so this step does not consume unaccounted worker image storage.

`hogline analyze` backfills locations from detailed frames without fetching video, changing existing stone observations, or accepting scores. Coarse activity frames are excluded. JSON releases include board locations and validate their source/evidence references.

## Local pilot

The automated backfill processed **99 detailed frames**, proposing locations in **81** and abstaining in **18**. The eleven retained coarse activity frames were excluded. Eight representative proposed crops were visually inspected, one per sheet. All eight enclosed the board, including the 1280×720 Sheet 2 recording whose board occupies only 218×70 original pixels. A separate Sheet 4 frame with a person blocking the yellow band correctly remained unavailable. These selected visual checks are development evidence, not a labeled archive-wide accuracy estimate.

Synthetic regression tests cover both source resolutions, missing blue headers, a large obstruction, competing boards, idempotent observations, crop checksums, unavailable image responses, export lineage, and exclusion of coarse frames. Different wall layouts, faded paint, advertisements, severe perspective changes, and partial obstructions need broader labeled validation.

## Proposed point columns and card reading

Version 0.10 adds `traditional-board-header-registration-v1`, used by `traditional-board-clock-v8` when an existing source-scoped template does not apply. The configured reference is the clear fixed-number header in [Sheet 1 at 00:05:00](https://www.youtube.com/watch?v=BXv6b907n7A&t=300s). Its fifteen point centers were measured from the source pixels. The original Sheet 4 reference has a movable card across part of the printed header, so it is unsuitable as the cross-sheet reference. Its existing scoped reader remains available.

The new method finds both boards independently, normalizes just their header bands, searches a small horizontal scale/translation range, and compares the entire fixed-number strip plus five separate sections. The storage numeral on the left is excluded. Current development thresholds are 0.82 overall correlation and 0.72 in every section. These are image-similarity gates, not calibrated probabilities. Mismatches produce no column mapping. A passing mapping transfers the reference point centers and uses the target's measured red/yellow row bands for card OCR.

The reference must already exist as retained detailed evidence. No reference or target video is fetched by OCR. The observation version includes the configuration and reference manifest; adding/restoring a missing reference creates a different extraction version, preserving the previous observations. Each read records the reference evidence ID, original image hash, source time, and YouTube link. Releases validate the reference and image checksum. The UI shows proposed column ticks, regional match diagnostics, source imagery, and a link to compare the fixed-header reference.

### Cross-sheet pilot

On the 99 retained detailed frames, the locator proposed 81 boards. Header registration alone passed 64 and declined 17 of those proposals. The combined reader, including the existing Sheet 4 source-scoped method, produced 85 board observations and left 14 unavailable. Many boards have no visible scoring cards; that is not evidence of a 0–0 score.

The January Sheet 5 crop at 00:30:00 visibly contains red end cards 1, 3, 4, 6, 7 at cumulative columns 1, 2, 3, 5, 6, and yellow end cards 2, 5 at columns 1, 3. OCR leaves the red 6 unreadable at that frame. Later consistent frames at 00:30:25–00:30:34.5 support four **proposed** red differences: end 3 +1, end 4 +1, end 6 +2, end 7 +1. These are posted-board observations; the posting time is not the delivery/end-completion time. They do not establish a final result or a complete game. Sheet 6 at 01:30:15 has readable red cards but people obscure portions of the yellow row; suspect occupied cells remain unreadable and prevent a complete yellow-row claim.

All eight sheets, two source resolutions, and two recording dates were exercised. Selected crops and proposed columns were visually checked, but no independent labeled-board benchmark exists yet. The current thresholds were developed using these samples. Changes in fonts, printed point ranges, storage conventions, perspective, or board design require new reference/configuration and validation. A repeated incorrect OCR read can still survive temporal agreement; automatic score acceptance is disabled.
