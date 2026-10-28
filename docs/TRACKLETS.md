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
