# Consistent releases and source provenance

`POST /api/releases` and `hogline export` freeze sources, scans, evidence, computer-vision and broadcast observations, recording-link proposals, game interpretations, and their revision numbers in one database transaction. The JSON schema is `hogline-archive-v2`. Existing release documents remain unchanged.

SQLite releases begin with `BEGIN IMMEDIATE`, reserving their write before reading. A concurrent worker or reviewer publishes after that release commits. Read-only catalog requests explicitly begin a read snapshot; WAL writers may commit while those requests continue to see their original view. Frozen evaluation datasets also use one publication transaction. This avoids Python's [legacy SQLite behavior](https://docs.sqlalchemy.org/en/20/dialects/sqlite.html#legacy-transaction-mode-with-the-sqlite3-driver), which otherwise does not begin a transaction for a SELECT.

The PostgreSQL path requests [REPEATABLE READ before the first statement](https://docs.sqlalchemy.org/en/20/orm/session_transaction.html#setting-isolation-for-individual-transactions). It has not been runtime-tested. This implementation currently supports SQLite and PostgreSQL only; deploying or evolving a shared database still requires migrations and operational validation.

JSON releases contain:

- `source_metadata_snapshots`: curated, content-hashed source manifests, including older versions. Credentials and temporary media URLs are excluded by the import adapter.
- `evidence_manifest_refs`: the acquisition manifests associated with each evidence frame, from its explicit pin and retained scan/job inputs.
- `lineage_gaps`: unpinned legacy frames and referenced manifest hashes whose original snapshots are unavailable. A current source hash never silently fills missing historical provenance.
- `processing_scans`: scan requests, source manifest pins, discrete samples, and job associations. A sparse scan does not establish continuous observation.
- `interpretation_revisions`: the exact game revisions visible to the release. Confirmed mode still excludes unreviewed scores and results.

Before inserting the immutable release row, publication verifies manifest checksums and source/evidence references. Missing frames, invalid cross-source associations, or corrupted manifests abort publication. Known legacy manifest gaps remain explicit in the release rather than changing old evidence. A release SHA-256 hashes the stored document, excluding the release ID and the separate returned hash field. CSV derives from the same frozen document and retains timestamp links; use JSON for complete provenance.

New frame identities include the source manifest hash. Reacquiring identical pixels after a metadata change therefore creates a new evidence identity while reusing the same content-addressed PNG. Existing evidence, labels, and interpretations keep their IDs.

Tests use coordinated concurrent transactions to verify that a release cannot mix old game revisions with new observations, and that a catalog read remains stable while a WAL writer commits. These checks establish local database consistency, not archive accuracy or PostgreSQL behavior.
