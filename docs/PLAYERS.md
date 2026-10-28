# Anonymous players and optional names

Open **Players · optional names** in the sidebar. Each indexed body fragment gets a persistent anonymous group ID; no roster or name is required. A group can contain several fragments. Naming a group updates its display name without changing its ID or original observations. The gallery shows body context only when a retained frame exactly matches a fragment's original PTS and crop geometry; otherwise use **Inspect source fragment** and its original source links.

This implements optional user naming and corrected grouping without face recognition, biometric embeddings or automatic cross-recording identification. A person can still have multiple groups, and a body proposal may be a spectator or someone on an adjacent sheet. Names supplied by the operator do not establish a thrower, delivery, team membership, or weight measurement.

## Automatic links

New detailed windows register their anonymous groups in the same transaction as their actor and delivery results. Existing results can be indexed in bounded batches through `POST /api/players/sync?after_id=CURSOR&limit=50`; the gallery does this when opened. A batch accepts at most 100 actor results and returns a cursor. Repeated registration preserves previous groups and manual decisions.

A fragment automatically joins an existing group only when it shares at least three original frame timestamps over one second with identical decoded-frame hashes and identical body boxes, in the same recording, view, camera profile and source metadata. Multiple matching groups remain separate. Overlap candidate searches are indexed and capped at 128; excessive candidates abstain. Clothing similarity, nearby positions across a gap, faces, names, or overlapping wall-clock dates do not establish a link. Legacy tracks missing decoded-frame hashes remain separate. Tracking version `perspective-actor-fragments-v3` retains those hashes.

The October 28 real-data check indexed 122 fragments from four retained actor analyses, with 90 groups having exact retained body previews. It assigned no names and made no cross-window links: the two sample cadences shared too few exact frames to meet the overlap rule. These are fragment counts, not 122 distinct people. Synthetic repeated-frame tests exercise positive overlap linking, changed-image abstention, and repeat indexing.

## Correcting a group

Expand a group's fragments, inspect their source evidence, then move a fragment to another existing group or **New anonymous player**, with a reason. This is reversible: move it back to the original group, whose ID and optional name are retained even when empty. Empty groups remain available as correction targets. A fragment belongs to one current group. No fuzzy automatic merge overwrites a manual assignment.

Names, notes, memberships and their prior values have checksummed revision history. Edits check the displayed membership, source-group and target-group revisions to reject stale/concurrent changes. `GET /api/players` reads the current gallery; `GET /api/players/history.json` includes history. `PUT /api/players/groups/ID` edits a name/notes; `PUT /api/players/fragments/ID` moves a fragment. OpenAPI documents request schemas. Thumbnails verify the source crop checksum before cropping a proposed body; they never infer or generate a face.

Immutable archive JSON releases include these groups, memberships and correction history beside the original actor analyses. Current player links are validated against the parent checksums, source IDs and fragment timestamps. Historical releases retain the names and assignments current at publication time. There are no accepted per-player delivery or weight statistics yet.
