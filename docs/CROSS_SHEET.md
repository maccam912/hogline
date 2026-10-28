# Cross-sheet source checks — October 27, 2026

Bounded metadata discovery found recordings for all eight sheets. The initial retained-media validation corpus covered ten recordings and 58 detailed frames, including the older January Sheet 5 stream. Coarse-tier checks added 11 activity-only frames. The detailed-window checkpoint below brings the local corpus to 94 retained frames: 83 detailed and 11 coarse. No complete video was downloaded or committed. Images, dense observations, and OCR outputs remain in the ignored data directory.

| Sheet | Source / title date | Selected frame resolution | Retained source seconds |
| --- | --- | --- | --- |
| 1 | [2026-03-17](https://www.youtube.com/watch?v=BXv6b907n7A&t=0s) | 1920 × 1080 | 300.0, 315.0, 330.0, 5400.0, 5415.0, 5430.0 |
| 2 | [2026-03-17](https://www.youtube.com/watch?v=bBsHR8TsKOc&t=0s) | 1280 × 720 | 300.0, 315.0, 330.0, 5400.0, 5415.0, 5430.0 |
| 3 | [2026-03-17](https://www.youtube.com/watch?v=KR0Cy0UhU9s&t=0s) | 1920 × 1080 | 300.0, 315.0, 330.0, 5400.0, 5415.0, 5430.0 |
| 4 | [2026-03-17 fragment](https://www.youtube.com/watch?v=es9urvYvLiE&t=0s) | 1920 × 1080 | 0.0, 15.0, 30.0, 300.0, 310.0, 315.0, 330.0 |
| 4 | [2026-03-17 long upload](https://www.youtube.com/watch?v=zN855hwsYxY&t=0s) | 1920 × 1080 | 300.0, 315.0, 330.0, 7395.0, 7410.0, 7425.0 |
| 5 | [2026-01-16](https://www.youtube.com/watch?v=976gtQn17P0&t=0s) | 1920 × 1080 | 1800.0, 1815.0, 1830.0 |
| 5 | [2026-03-17](https://www.youtube.com/watch?v=qBE25ciL0K4&t=0s) | 1920 × 1080 | 300.0, 315.0, 330.0, 5400.0, 5415.0, 5430.0 |
| 6 | [2026-03-17](https://www.youtube.com/watch?v=bxsiCiRMBUk&t=0s) | 1920 × 1080 | 300.0, 315.0, 330.0, 5400.0, 5415.0, 5430.0 |
| 7 | [2026-03-17](https://www.youtube.com/watch?v=pa3KvqDY-6U&t=0s) | 1920 × 1080 | 300.0, 315.0, 330.0, 5400.0, 5415.0, 5430.0 |
| 8 | [2026-03-17](https://www.youtube.com/watch?v=49-UH9e-uF0&t=0s) | 1920 × 1080 | 300.0, 315.0, 330.0, 5400.0, 5415.0, 5430.0 |

## What differs

- Sheet 2's selected encode is 1280×720; the others sampled here are 1920×1080. Normalized crops scale to the actual image, giving 320-pixel versus 480-pixel house views. A resolution increase cannot recover missing detail.
- All eight March samples contain the expected two central overhead views, but house centers, apparent ring diameters, exposure, center logos, and nearby occlusions differ. The OpenCV fits are per frame/view, with no shared metric calibration or physical end naming.
- Scoreboard framing varies across the perspective cameras. Only the two inspected March Sheet 4 recordings have an applicable template, checked against retained header evidence on each reading. Their early/late board framing also differs by a few pixels; see [recording links](RECORDING_LINKS.md). Other sheets explicitly return `sheet_scoreboard_template_not_configured`; a plausible-looking wrong crop is not treated as evidence.
- Equal source seconds are not equal club time. The Sheet 4 fragment has a later overlay time than the beginnings of the other March recordings.
- Sheet 2 has poorer OCR and sampled time reads that do not fit one continuous offset. This may involve OCR error or source/encoder timing discontinuities; no cause is asserted. The segment fitter declines a supported mapping for these samples rather than correcting their dates from the title.
- The older January Sheet 5 date currently fails OCR although its time is read. Its date and wall-time mapping stay unresolved.

## Activity and score checks

Early samples at source 300–330 seconds include mostly empty houses on the other seven sheets. Later samples at 5400–5430 seconds show actual play and stones on Sheets 3, 5, and 6, with quiet ice remaining on several others. Thus the corpus includes negative examples, activity, occlusion, and different resolution/exposure; it is still far too small and selectively sampled for an archive-wide accuracy claim.

On the Sheet 4 sample, the OCR recognizes red end cards 1, 2, and 5 at cumulative columns 2, 3, and 5, respectively. Red-row agreement across four frames yields **proposals** of red +1 in end 2 and red +2 in end 5. A partially occluded or conflicting yellow row does not erase the independently readable red row. Earlier unobserved totals, blank ends, and the final result remain unknown. These are review inputs, not independently verified game results.

The broadcast clock lacks AM/PM. Both twelve-hour alternatives remain in the proposed clock mappings. Samples crossing a gap, contradictory overlay, or unreadable clock do not establish a continuous mapping across it.

## Remaining validation

### Detailed-window checkpoint

The v0.7 decoder/temporal path was exercised on twelve ten-second intervals using `handle-configuration-persistence-v2`:

| Recordings | Source interval | Detailed samples per interval | Outcome |
| --- | --- | --- | --- |
| All eight March sheets, using the Sheet 4 fragment | 300–310 seconds | 20 | Decoded and preserved source PTS at native selected resolution |
| January Sheet 5 | 1800–1810 seconds | 20 | Dark foreground gate rejects a persistent person’s foot/clothing candidate seen in v1 |
| March Sheets 3, 5, and 6 during play | 5400–5410 seconds | 20 | Decoded; foreground/clipping/motion checks prevent a whole-configuration proposal |

These twelve current runs produced 480 overhead-view rows in 219,072 bytes of Parquet, with 142.6 total worker seconds on this machine. Every sampled PTS was increasing and inside its requested interval; every artifact checksum and release reference verified. This is a small bounded execution check, not a season-scale throughput estimate. Earlier development runs remain in local history, giving 21 window manifests in total at this checkpoint.

The current conservative persistence gate accepted **zero configuration proposals** across these intervals. Some views had no handle candidates; active views had clipped candidates, possible obstruction, or changing detections. This is not evidence that the houses were empty, nor a usable automatic end-scoring accuracy result. A stationary foreground false positive in the earlier January run motivated the dark-region gate; that case is now development data. Further work must improve recall and visibility modeling against independently labeled examples, not merely lower thresholds until this small corpus passes. See [window contracts and limits](WINDOWS.md).

### Larger evaluation corpus

A second acquisition tier was exercised on the same active/quiet source moments on Sheets 2 and 3: three coarse samples at 5400, 5415, and 5430 seconds per recording. The retained coarse images are 640×360, separately identified from the existing 1280×720 and 1920×1080 detailed evidence. They are restricted to activity screening; no handle or scoreboard observations are published from them. These six new images are local cache data, not repository assets.

Five more coarse frames from the older January Sheet 5 recording exercise bounded planning and pause/resume across adjacent central intervals. Details and actual costs are in the [processing checkpoint](PROCESSING.md).

Fully label a long day and a confirmed overlapping/continuation pair. Add per-sheet board templates with dated validity episodes; independently annotate end/score truth, missed stones, people/clothing false positives, and boundary-clipped handles. Split evaluation by recording day/game and duplicate-upload family. The synthetic regression suite and these source smoke checks do not establish the design's release gates.
