# Bounded recording metadata refresh

Channel discovery can leave a recording with listing-only metadata or flag changes to a previously imported recording. **Discover sources → Complete recording metadata** queues up to 16 flagged YouTube recordings at a time (default four), with a 5–60 second timeout per request (default 30). A background worker runs the requests individually. It does not download video, queue scans, or begin a season backfill.

The panel shows how many recordings still need full metadata and recent job outcomes. The selected source's Processing section also shows its metadata jobs. Failed jobs retain the prior source and require an explicit retry, with at most three attempts. Queueing again advances to other eligible sources; it cannot reset a failed job's attempt count, even with a different timeout. An active source is never queued twice.

Each request pins the complete source record. A new listing, manual import, or other source edit before/during the request rejects the obsolete response. Queue current work after that failure. A reclaimed worker lease cannot publish. The new manifest and successful job outcome commit in one transaction, with cumulative attempt-time accounting. The metadata request timeout bounds the subprocess; database publication overhead is additional. These requests share the existing local worker and may wait behind media work.

Curated snapshots retain format metadata but omit signed media URLs, cookies, and request headers. Prior snapshots and existing game/evidence revisions remain intact. A changed manifest intentionally makes prior analyses earlier-input results; a metadata refresh does not recalculate or accept them. Failed or inaccessible requests do not assert that the recording has been deleted.

```sh
uv run hogline refresh-metadata --preview
uv run hogline refresh-metadata --limit 4 --timeout 30
uv run hogline work --once
```

`GET /api/metadata-refresh` returns pending sources and the latest 50 jobs. `POST /api/metadata-refresh` accepts `limit` and `timeout_seconds`. The worker runs while `hogline serve` is open, or through the existing `work` command. There is no implicit schedule for full refreshes.

Regression tests cover bounded selection, active-job deduplication, unchanged failure limits, timeout forwarding, metadata sanitization, old manifests, stale source/lease rejection, API validation, and compatibility with media-cache lookup and dataset export. The separate real-source trial refreshes one public Sheet 4 listing in an isolated local database, without modifying the main archive.

The live workbench also refreshed four flagged recordings on Sheets 1, 4, 5, and 6: all succeeded on their first attempt, all full-manifest hashes remained unchanged, and the pending count fell to zero. Repeating the queue action created no duplicate jobs. Browser checks exposed stale script caching; HTML now references content-hashed assets and requests cache revalidation so updated controls match the server.
