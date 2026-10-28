# Cross-sheet source checks — October 27, 2026

Bounded metadata discovery found recordings for all eight sheets. The local corpus now contains nine recordings and 49 retained original frames, including the older January Sheet 5 stream. No complete video was downloaded or committed. Images and OCR outputs remain in the ignored data directory.

| Sheet | Source / title date | Selected frame resolution | Retained source seconds |
| --- | --- | --- | --- |
| 1 | [2026-03-17](https://www.youtube.com/watch?v=BXv6b907n7A&t=0s) | 1920 × 1080 | 300.0, 315.0, 330.0, 5400.0, 5415.0, 5430.0 |
| 2 | [2026-03-17](https://www.youtube.com/watch?v=bBsHR8TsKOc&t=0s) | 1280 × 720 | 300.0, 315.0, 330.0, 5400.0, 5415.0, 5430.0 |
| 3 | [2026-03-17](https://www.youtube.com/watch?v=KR0Cy0UhU9s&t=0s) | 1920 × 1080 | 300.0, 315.0, 330.0, 5400.0, 5415.0, 5430.0 |
| 4 | [2026-03-17](https://www.youtube.com/watch?v=es9urvYvLiE&t=0s) | 1920 × 1080 | 300.0, 310.0, 315.0, 330.0 |
| 5 | [2026-01-16](https://www.youtube.com/watch?v=976gtQn17P0&t=0s) | 1920 × 1080 | 1800.0, 1815.0, 1830.0 |
| 5 | [2026-03-17](https://www.youtube.com/watch?v=qBE25ciL0K4&t=0s) | 1920 × 1080 | 300.0, 315.0, 330.0, 5400.0, 5415.0, 5430.0 |
| 6 | [2026-03-17](https://www.youtube.com/watch?v=bxsiCiRMBUk&t=0s) | 1920 × 1080 | 300.0, 315.0, 330.0, 5400.0, 5415.0, 5430.0 |
| 7 | [2026-03-17](https://www.youtube.com/watch?v=pa3KvqDY-6U&t=0s) | 1920 × 1080 | 300.0, 315.0, 330.0, 5400.0, 5415.0, 5430.0 |
| 8 | [2026-03-17](https://www.youtube.com/watch?v=49-UH9e-uF0&t=0s) | 1920 × 1080 | 300.0, 315.0, 330.0, 5400.0, 5415.0, 5430.0 |

## What differs

- Sheet 2's selected encode is 1280×720; the others sampled here are 1920×1080. Normalized crops scale to the actual image, giving 320-pixel versus 480-pixel house views. A resolution increase cannot recover missing detail.
- All eight March samples contain the expected two central overhead views, but house centers, apparent ring diameters, exposure, center logos, and nearby occlusions differ. The OpenCV fits are per frame/view, with no shared metric calibration or physical end naming.
- Scoreboard framing varies across the perspective cameras. Only the inspected March Sheet 4 template is configured. Other sheets explicitly return `sheet_scoreboard_template_not_configured`; a plausible-looking wrong crop is not treated as evidence.
- Equal source seconds are not equal club time. The Sheet 4 fragment has a later overlay time than the beginnings of the other March recordings.
- Sheet 2 has poorer OCR and sampled time reads that do not fit one continuous offset. This may involve OCR error or source/encoder timing discontinuities; no cause is asserted. The segment fitter declines a supported mapping for these samples rather than correcting their dates from the title.
- The older January Sheet 5 date currently fails OCR although its time is read. Its date and wall-time mapping stay unresolved.

## Activity and score checks

Early samples at source 300–330 seconds include mostly empty houses on the other seven sheets. Later samples at 5400–5430 seconds show actual play and stones on Sheets 3, 5, and 6, with quiet ice remaining on several others. Thus the corpus includes negative examples, activity, occlusion, and different resolution/exposure; it is still far too small and selectively sampled for an archive-wide accuracy claim.

On the Sheet 4 sample, the OCR recognizes red end cards 1, 2, and 5 at cumulative columns 2, 3, and 5, respectively. Red-row agreement across four frames yields **proposals** of red +1 in end 2 and red +2 in end 5. A partially occluded or conflicting yellow row does not erase the independently readable red row. Earlier unobserved totals, blank ends, and the final result remain unknown. These are review inputs, not independently verified game results.

The broadcast clock lacks AM/PM. Both twelve-hour alternatives remain in the proposed clock mappings. Samples crossing a gap, contradictory overlay, or unreadable clock do not establish a continuous mapping across it.

## Remaining validation

A second acquisition tier was exercised on the same active/quiet source moments on Sheets 2 and 3: three coarse samples at 5400, 5415, and 5430 seconds per recording. The retained coarse images are 640×360, separately identified from the existing 1280×720 and 1920×1080 detailed evidence. They are restricted to activity screening; no handle or scoreboard observations are published from them. These six new images are local cache data, not repository assets.

Fully label a long day and a confirmed overlapping/continuation pair. Add per-sheet board templates with dated validity episodes; independently annotate end/score truth, missed stones, people/clothing false positives, and boundary-clipped handles. Split evaluation by recording day/game and duplicate-upload family. The synthetic regression suite and these source smoke checks do not establish the design's release gates.
