# Incremental source discovery

**Discover sources** registers public YouTube channel streams/videos listings or playlists and queues metadata pages. A discovered item, a full recording manifest, a cached image, a completed media job, and an accepted game remain separate facts.

Each feed keeps a persistent positional cursor, generation, overlap anchors, scheduling settings, and page runs. The adapter uses bounded yt-dlp `--playlist-items` ranges with flat/lazy extraction and skip-download mode; see the [upstream video-selection documentation](https://github.com/yt-dlp/yt-dlp#video-selection). Pages request at most 50 positions within a 10,000-position scope. Each request has a 120-second timeout and a 240-second work lease.

The response is curated and hashed before import. Signed media URLs, headers, and cookies are omitted. An interrupted import reuses the acquired page. Sources deduplicate by platform/video ID, with separate feed membership. Invalid IDs and unreadable placeholders are recorded without discarding valid entries from the page. Repeated pages, missing overlap anchors, and unexpected provider positions stop trusted cursor advancement.

## Advancing and reconciling

Adding a feed queues its initial page budget (two by default, at least the first explicit page). Adjacent pages share three entries by default. **Next page** adds one page from the cursor. **Check latest page** reads the head without changing the cursor. **Restart listing pass** creates a new generation at position one while retaining history.

With scheduling enabled, the local server checks the head every 24 hours by default and admits the declared backfill-page budget. A pass previously at the visible end starts a fresh generation at its next cycle. Scheduling works while the server is running, is off by default, and can be toggled on an existing feed.

Pause stops further requests/import stages. An in-progress request may take up to its timeout to return; its response remains available for resume. Pauses do not consume failure attempts. Failures use 60/120-second backoff, at most three attempts, then `needs_attention`. Expired leases can be reclaimed, and a superseded token cannot fetch or publish. Restart cancels queued/failed old-generation work; an already-running old-generation page cannot advance the new cursor.

YouTube positions are not stable continuation tokens. Overlap and periodic head/fresh-pass reconciliation reduce omissions but do not prove archive completeness during reordering or edits. Missing overlap or a repeated page sets `needs_reconcile`, preserving the cursor. The position cap leaves the archive end unknown. A short response records only `visible_end_observed` for that retrieval.

At a visible pass end, known members absent from that generation appear in `unseen_in_visible_pass_source_ids` and are marked for metadata refresh. Their availability, source rows, evidence, games, and interpretations are preserved. Listing absence does not establish deletion/private status. Full manifest refresh is still separate: `metadata_refresh_needed` is tracked and reimporting a recording URL refreshes it; automatic full-refresh budgeting remains to add.

```sh
uv run hogline discover 'https://www.youtube.com/@club/streams' --page-size 12 --overlap 3 --pages 2
uv run hogline discovery-work --once
uv run hogline discovery
uv run hogline discovery-control FEED_ID next
uv run hogline discovery-control FEED_ID head
uv run hogline discovery-control FEED_ID configure --scheduled
uv run hogline discovery-control FEED_ID configure --no-scheduled
uv run hogline discovery-control FEED_ID pause
uv run hogline discovery-control FEED_ID resume
uv run hogline discovery-control FEED_ID restart
```

`serve` processes discovery alongside media jobs; `discovery-work` handles metadata independently. An existing canonical feed URL reuses its saved configuration. API routes are `GET /api/discovery`, `POST /api/discovery/feeds`, `POST /api/discovery/feeds/{id}/control`, and `GET /api/discovery/runs/{id}`. The last exposes the curated response/checksum; summaries omit response bodies. State stays in the ignored local data directory.

## Real-source checkpoint

A browser-created, unscheduled Wausau feed requested positions 1–3 and 3–5 with one shared entry. Both runs succeeded, observed five distinct recordings, and advanced the cursor to position five with supported overlap. A 7,456-second March 17 Sheet 4 upload was new to the prior inventory and is a candidate for content comparison with the short fragment. The inventory grew from nine to ten sources. Retained images stayed at 60 and media jobs at 21. Discovery fetched no video or image.

Failure tests cover sanitized pages, resumable import, pause/resume, restart generations, stale leases, retries/backoff, drift/repeated pages, absence reporting, bounded positions, and HTTP inspection. Season-wide completeness, edited/deleted-source accuracy, and multi-day scheduled throughput still need a larger operational corpus.
