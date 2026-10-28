# Automatic board location

`traditional-board-color-bands-v1` finds a red row, a blue fixed-number header, and a yellow row within the upper end-wall portion of the retained left perspective view. It measures their geometry in each image instead of copying Sheet 4's crop coordinates to another sheet. This is a Wausau layout prior, not a general scoreboard detector.

The workbench shows the proposed crop under **Clock & scoreboard observations**, even when no scoring template applies. Multiple similarly plausible candidates remain ambiguous; missing or obstructed bands leave the location unavailable. Point columns, storage markers, blank-end conventions, and card meanings remain unknown. Existing source-scoped OCR templates and temporal score checks remain separate.

## Evidence and storage

Every observation retains the original image hash, evidence/source IDs, source metadata hash, original PTS and rational timebase, exact normalized crop bounds, derived RGB hash, dimensions, method version, and timestamped YouTube link. The HTTP crop is derived from the original image on demand and verifies both hashes. Missing or corrupted originals return an explicit unavailable response. No additional crop image is retained, so this step does not consume unaccounted worker image storage.

`hogline analyze` backfills locations from detailed frames without fetching video, changing existing stone observations, or accepting scores. Coarse activity frames are excluded. JSON releases include board locations and validate their source/evidence references.

## Local pilot

The automated backfill processed **99 detailed frames**, proposing locations in **81** and abstaining in **18**. The eleven retained coarse activity frames were excluded. Eight representative proposed crops were visually inspected, one per sheet. All eight enclosed the board, including the 1280×720 Sheet 2 recording whose board occupies only 218×70 original pixels. A separate Sheet 4 frame with a person blocking the yellow band correctly remained unavailable. These selected visual checks are development evidence, not a labeled archive-wide accuracy estimate.

Synthetic regression tests cover both source resolutions, missing blue headers, a large obstruction, competing boards, idempotent observations, crop checksums, unavailable image responses, export lineage, and exclusion of coarse frames. Different wall layouts, faded paint, advertisements, severe perspective changes, and partial obstructions need broader labeled validation.
