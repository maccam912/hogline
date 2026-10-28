# Split and merge game interpretations

Open a saved game in **Review**, then **Split or merge**. Save or close unsaved game/label edits first. Preview the replacement grouping, enter a reviewer and reason, and save the correction. Source video, retained frames, raw observations, and prior releases are unchanged.

## Splitting

Supply a source time for every linked recording. The workbench does not assume their clocks are synchronized. A zero cut puts that recording's spans in the later game; a cut at its duration puts them in the earlier game. Both new games must have at least one nonempty span. Every part of every original interval is preserved.

An end whose supporting frames all fit only one child is assigned there as a proposal. Ends without enough placement evidence require an explicit choice. An end supported by frames across the split cannot be moved by discarding its evidence: adjust the boundary or correct that end's evidence in the game editor first. Official end numbers are retained; observed indices are resequenced within each child.

## Merging

Select other active games on the same sheet. Their actual source intervals and relationships are retained; exact duplicate spans are removed. A gap between fragments stays a gap. Conflicting team/league fields become unidentified rather than arbitrarily choosing a parent.

Every duplicate known official end requires a choice of which interpretation to carry forward. The other interpretation stays in parent history and the end mapping explicitly marks it superseded. Unknown-numbered ends stay separate. The operation never silently combines inconsistent scores or drops an unmatched end.

## Review status, history, and undo

Each correction creates new game IDs and retires the parents into those successors. Parent snapshots retain their scores, evidence, original review decisions, and final results. Successor boundaries are proposed, coverage is partial, completion/result are unknown, and carried human-confirmed ends become proposed pending review in their new game context. Unknown and disputed ends keep their status. Old review tasks close; the new grouping gets review tasks.

**Game correction history** shows the before/after groups with timestamp links. **Restore previous grouping** appends new revisions; it never erases the correction. Restore is rejected if any related game has been edited or reconciled again, so it cannot overwrite later work. A new reviewer/reason is required. The previous grouping, including its original review decisions, is restored only when these guards pass.

Publication compares every parent revision and the exact preview hash, including the linked source metadata hashes, then saves the children, retirements, end mappings, and operation record in one transaction. Retrying the same publish request returns the original operation. Failed publication rolls everything back. Ordinary game edits cannot set retirement links or resurrect retired rows.

JSON releases contain active `games`, `retired_games`, `reconciliations`, and exact interpretation revision numbers. Historical snapshots are audit material even in a confirmed-mode export; only the eligible active `games` participate in current metrics. CSV continues to represent the selected active-game release; use JSON for correction lineage.

## Validation and limits

Regression fixtures cover splits with placed and ambiguous ends, evidence crossing a cut, multi-recording cuts, duplicate official-end resolution, prior final-result invalidation, stale source/parent revisions, idempotent publication, rollback after child creation, cross-sheet rejection, and refusal to undo later edits. Browser validation in a separate synthetic catalog exercised split → undo and conflicting-end merge → undo, including resolving choices across multiple previews. The real pilot game records were not changed by these checks.

This is operator-driven reconciliation, not automatic game discovery. It supports a one-to-two split or a two-to-ten-parent merge. It does not align unsynchronized recordings, combine multiple conflicting end readings into a new score, or infer identities. More complex corrections can be composed as separate reviewed operations, each with its own lineage.
