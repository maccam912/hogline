# Handle fragments

Detailed windows now derive short image-space associations automatically. Existing current-format windows can use **Trace handles from cached observations**, or `uv run hogline trace-window WINDOW_ID`. This reads retained Parquet only; it does not resolve media, download video, or retain more images.

Each proposed fragment contains at least three consecutive observations spanning one second. It records crop-relative handle centers, color, original source seconds, integer PTS and timebase, decoded-frame hash, detector index, and a YouTube timestamp for every point. The viewer shows net pixel displacement and the discrete points, with a complete JSON download. Lines connect samples for inspection; the intervening path is unknown.

## Association and abstention

`conservative-handle-tracklets-v1` uses same-color assignment with bounded displacement and constant-velocity prediction. Nearby same-color candidates and close competing assignments split fragments. Missing observations, clipped candidates, sampled gaps, crop changes, missing house references, and possible foreground/ring obstruction also prevent continuity. There is no cross-camera or cross-window identity fusion. Small pixel changes can be detector jitter, lighting, or camera motion; even a larger displacement is only a proposed position change.

The method does not establish a physical stone identity, delivery, collision, shot outcome, calibrated speed, house footprint, score, or end phase. Hidden stones and undetected movement remain unknown. These fields remain null in exports. All fragments are proposed and cannot promote a game or score to confirmed.

## Reproducibility and limits

The derivation checks the retained dense artifact's size, SHA-256, schema, manifest, source, crop profile, detector/visibility versions, sample pairing, coordinates, and monotonic PTS timing. Results have immutable versioned IDs and content hashes, and preserve their exact source manifest and input-Parquet reference. Repeating the request reuses the same result. JSON releases include all fragments and validate their window/anchor lineage. Context anchors are the window's retained images; they are not images of every track sample.

Input remains limited to 240 samples in two views and a 16 MiB Parquet artifact. A view with more than 64 candidates is excluded. Derived output is limited to 2 MiB; exceeding this publishes an explicit abstention with no partial fragment set and preserves the original window. The viewer renders up to 50 fragments, prioritizing position changes; downloads include the entire result.

Synthetic regression tests cover motion, stationary handles, ambiguity, crowded contacts, missing samples, color changes, clipping, short runs, obstruction, timestamp and checksum corruption, idempotent cached derivation, automatic window integration, output-size abstention, and release lineage. Real-data checks and remaining limitations are recorded below; synthetic continuity is not a real-world tracking accuracy claim.

## Real-data check, October 28, 2026

Twenty-one previously cached current-format windows on Sheets 2, 4, and 6 produced 45 fragments, all below the net-motion threshold. Five additional 12-second windows at source 01:30:00 on Sheets 1, 3, 5, 7, and 8 completed automatically at a requested 0.25-second interval. Each retained 45 actual samples per view; the decoder preserves actual timestamps rather than inventing the requested count. Sheet 3 produced three further fragments; the other four produced none. All eight sheet layouts were exercised, including the 720p Sheet 2 source.

These 26 windows produced 48 proposed fragments and **no fragments above the net-motion threshold**. This validates cached derivation, automatic acquisition integration, abstention, and source links across the layouts; it does not validate moving-stone recall or identity accuracy. The tested intervals are mostly quiet or obstructed. A representative independently labeled set of deliveries, collisions, and occlusions remains necessary. The stationary Sheet 4 fragment viewer and exact PTS links were inspected in the browser. Images, Parquet, result JSON, and working reports remain local in ignored data/.

The integration suite also generates two seven-second local lossless videos at 1280×720 and 1920×1080. The actual FFmpeg decoder, HSV detector, visibility check, Parquet writer, tracker, search, and release export run without mocked inference. A red handle moves through both overhead views while yellow remains fixed; a one-second foreground obstruction interrupts only the top view. Both resolutions produce the expected two top fragments and one continuous bottom fragment, retain original PTS, recover known synthetic centers within one pixel, and never create a YouTube identity for local video. These controlled fixtures establish pipeline behavior, not real-footage accuracy.

A subsequent bounded real-data check analyzed two one-minute intervals: Sheet 4 March 17 at 00:00:00 and Sheet 5 January 16 at 03:00:00. The Sheet 5 window produced 47 fragments, seven above the net-motion threshold, between 03:00:24.533 and 03:00:33.333 in the bottom view. Additional single frames at 03:00:24.533, 03:00:28, and 03:00:33.333 were inspected: yellow stones are being repositioned near the left house edge while red stones remain on the right. This is a useful real movement example, not seven delivered shots or verified stone identities. Partial people remain a limitation of the heuristic visibility check.

Moment-search navigation now opens the exact selected fragment first, including its stored tracker version. The fragment viewer also exposes timestamped window-context images separately, so a path is never drawn onto a frame from a different instant. The selected 77.4-pixel yellow fragment and its source links were checked in the browser.
