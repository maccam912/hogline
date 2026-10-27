# Initial pilot, October 27, 2026

The first implementation imported the two recordings requested in DESIGN.md as metadata only:

| Source | Metadata duration | Requested scan | Retained frame times |
| --- | --- | --- | --- |
| [Sheet 4, March 17](https://www.youtube.com/watch?v=es9urvYvLiE) | 2,210 seconds | 300–345 seconds | 300, 315, 330 seconds |
| [Sheet 5, January 16](https://www.youtube.com/watch?v=976gtQn17P0) | 36,256 seconds | 1,800–1,845 seconds | 1,800, 1,815, 1,830 seconds |

No complete stream was downloaded. FFmpeg sought individual full-resolution frames over the selected media URL. Original PTS used a 1/15360 timebase in these selected encodes. Metadata and small frame/crop caches are in the local ignored `data/` directory. Temporary signed stream URLs and request credentials are not persisted in metadata snapshots.

Examples: [Sheet 4 at 5:00](https://www.youtube.com/watch?v=es9urvYvLiE&t=300s), [Sheet 5 at 30:00](https://www.youtube.com/watch?v=976gtQn17P0&t=1800s).

Sparse motion proposed one partial interval in each scan. These are **unreviewed activity**, not established game boundaries. No scores, final results, leagues, named participants, wall clocks, or calibration were accepted. This does not establish whole-day recall, game count, or scoreboard feasibility.

The OpenCV color/shape detector was run on the six retained frames without fetching video again. Its overlays are proposals. Visual inspection of the Sheet 4 5:00 overhead crop reveals a partly clipped red stone connected to the top border that the baseline misses. A regression fixture also caught the contour-hierarchy problem where a red border can hide interior red components; retrieval now retains inner contours. Neither this fix nor a few plausible detections establishes accuracy on Wausau footage.

Runtime validation covers SQLite, the local API/UI, FFmpeg PTS capture, bounded remote frame access, and cached OpenCV inference. PostgreSQL schema compatibility is an implementation choice, not a tested deployment. A confirmed overlapping/continuation recording pair, a fully labeled multi-game day, camera calibration, and automatic scoreboard/end scoring are the next substantive validation tasks.
