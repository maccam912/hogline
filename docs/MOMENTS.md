# Find moments across recordings

**Find moments** searches retained evidence without acquiring media. Filter descriptions, sheet, recording, title-date range, moment kind, and review state. Each card links the exact source and both interval endpoints. **Inspect in workbench** opens the source viewer, the before/after board comparison, or the relevant handle-fragment result. Existing game/handle-label drafts are guarded before navigation.

The searchable kinds are posted-board changes, proposed cumulative-column score differences, handle fragments with net position change, persistent house configurations, and saved played/blank end interpretations. Stationary handle fragments remain in their window viewer. Retired game interpretations are excluded. A human-confirmed end filter refers to the end review; it does not confirm its game's boundaries or final result.

Result intervals have different meanings. A board change brackets when posted cards changed; it is not an end-completion timestamp. A score proposal gives repeated supporting board observations. A saved end gives the times of its supporting frames. A handle fragment gives its sampled observations; retained window images are context and may lie outside that fragment. These meanings stay on each result card. No result creates or confirms a score, game, shot, or identity.

By default, results from earlier source manifests or older detector/tracker/visibility methods are hidden. Enable **Include earlier source or analysis inputs** to inspect those explicitly. Only the current board reader is indexed; historical reader outputs remain in the stored observations and releases. Different windows or repeated board states can describe the same play. Result counts are evidence records, not counts of unique physical events or games.

Title dates are labels parsed from recording titles. They are not a wall-clock mapping, event date, or proof that every moment happened on that calendar day. Missing dates are excluded when a date bound is set. Search reports both inventoried and indexed recording counts, and a zero-match result never establishes that no play occurred.

Pages contain at most 100 records (25 in the viewer). The index fingerprint includes every indexed result. Paging supplies the previous fingerprint and fails if the underlying evidence or interpretations changed, preventing silently inconsistent page combinations. **Download this result page** saves its filters, fingerprint, coverage, and source links; it is a page of search results, not a frozen dataset release. Use **Export dataset** for a durable interpretation snapshot.

```sh
uv run hogline find-moments --sheet 4 --kind board_change
uv run hogline find-moments --text 'red end 5' --limit 50
```

`POST /api/moments/search` accepts `MomentQuery` (shown in the local API documentation), including title-date bounds, source ID, offset, and `expected_index_sha256`. It performs a read-only database snapshot. Tests cover exact bracket/source links, proposed/confirmed separation, date/sheet/text/kind filtering, earlier inputs, pagination drift, retired games, stationary-fragment exclusion, and request validation. Browser checks on the local corpus found 36 current-input moments and successfully navigated a board-change result to its original before/after images and YouTube links.
