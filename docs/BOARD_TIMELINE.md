# Posted board changes

The recording view now has a **Posted board changes** timeline. It groups repeated card readings separately for red and yellow, then compares consecutive stable row states. **Compare board evidence** shows both board crops, full-frame disclosures, and direct YouTube links to the last earlier observation and first later observation.

The interval between those anchors is an uncertainty bracket. It is not an exact event timestamp, an end-completion time, or a game boundary. Cards may be posted late, moved for a correction, obscured, or cleared. A row with no readable cards does not establish a zero score or clear visibility. Added cards, removed readings, moved card positions, and mixed changes remain proposed observations without a score, game ID, or accepted result.

`posted-board-change-brackets-v1` requires:

- At least three distinct source timestamps and evidence frames spanning two seconds for each row state.
- No unreadable occupied slots or conflicting end numbers in that row.
- At most 90 seconds between observations within a state. An unreadable row breaks its run independently of the other color.
- One source recording, metadata snapshot, and OCR version. Legacy readings without a source-manifest pin are excluded from this timeline; their original observations and separate export lineage remain available.

Alternative evidence at the same source time adds no votes. Conflicting alternatives break the run. Repeated use of one frame cannot establish persistence. A long interval between two stable states remains visible as `sparse_gap`; unreadable intermediate samples are counted and labeled `interrupted_readings`. The comparison does not imply that only one change occurred in the gap.

The JSON dataset includes `broadcast.board_timeline`, with method version, thresholds, row epochs, evidence/observation IDs, before/after anchors, card differences, and explicit unknown fields. Export validation checks source references, epoch membership, and exact anchor correspondence. Confirmed-result metrics do not use these proposals.

The retained real-media check initially produced eight row epochs and one broad Sheet 4 change bracket from source time 330 to 7395 seconds. Its large uncertainty is displayed directly. Both source crops and YouTube links were checked in the browser, and an exploratory dataset export passed reference validation. This is a workflow check, not independently measured card-change detection accuracy. Synthetic cases cover card additions/removals/repositioning, unreadable rows, sparse gaps, duplicates, conflicting reads, version/metadata boundaries, unknown scores, and corrupt export references.
