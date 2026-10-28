# Layout checks, clock alternatives, and recording links

The March 17 Sheet 4 long upload and short fragment illustrate why a recording boundary cannot define a game boundary. The app now proposes a link from independently retained source evidence, keeps the missing interval explicit, and can seed a two-source game draft. Game membership, completion, scores, and final results remain separate review decisions.

## A real boundary candidate

- Earlier recording: [long Sheet 4 upload at 02:03:45](https://www.youtube.com/watch?v=zN855hwsYxY&t=7425s). Its retained late samples at 7395, 7410, and 7425 source seconds show overlay times 09:48:58, 09:49:13, and 09:49:28.
- Later recording: [Sheet 4 fragment at its start](https://www.youtube.com/watch?v=es9urvYvLiE&t=0s). Samples at 0, 15, and 30 seconds show 09:52:48, 09:53:03, and 09:53:18.
- Three consecutive samples in each window support offset hypotheses. Both AM/PM interpretations remain; upload dates and title dates do not select one.
- Using the full-metadata duration of 7455 seconds, the estimated recording gap is **170 seconds**. The nearest retained samples are **200 seconds** apart, including 30 seconds of extrapolation to the earlier recording's declared end. The displayed ±3-second arithmetic uncertainty is conditional on clock continuity and declared duration; a clock change during unobserved footage remains unknown.
- The readable red row has the same posted end-card configuration across at least two distinct times per recording. Yellow-row occlusion/contradiction prevents equivalent support. Ongoing play and matching posted state support a **possible continuation**, not a confirmed canonical game or winner.

These checks added nine detailed frames to the prior cache: six from the long upload and three at the fragment's beginning. The retained corpus now has ten recordings, 58 detailed frames, and 11 coarse frames (69 total). No complete video was downloaded. No new game or result was saved during the comparison/draft test.

## Recording-scoped board alignment

The default board template applies only to explicit source/time intervals for these two inspected recordings. Other uploads and sheets need their own applicable layout configuration. A sheet number alone cannot select a board crop.

Each board reading checks its fixed numbered header against the retained fragment reference at source second 300. The reference is an integrity-checked local evidence asset; image bytes are not embedded in the public repository. If it is missing or corrupt, board reading abstains. Import/cache that one reference frame to enable the seed template in a new local dataset.

The check uses [OpenCV phase correlation](https://docs.opencv.org/4.x/d7/df3/group__imgproc__motion.html) on a normalized fixed-header patch. A bounded translation can be proposed, refined over neighboring source pixels, and checked again before OCR. Rotation, larger shifts, texture loss, occlusion, or weak agreement decline the reading. Original and corrected board crops, effective bounds, reference hash, shift, and residual diagnostics remain inspectable.

The long recording's early samples actually need a proposed **1-pixel horizontal / 3-pixel vertical** crop translation relative to the fragment reference. Header correlation rises from roughly 0.27–0.34 to 0.85–0.86 after correction. Its late samples fit the original seed more closely. This is an experimental gate with unvalidated thresholds, not an archive-wide layout-accuracy result or surveyed geometric calibration. It does not authorize automatic score acceptance.

## Competing clock readings

`traditional-board-clock-v7` retains a primary thresholded read and a grayscale alternative. The secondary date crop trims a small left margin; each crop and transform is stored. At long-source second 315, the original date read is `09/17/2026`, while the tighter grayscale read is `03/17/2026`. The first time read fails; the grayscale alternative reads `07:50:58`. Both valid date interpretations remain in that frame. Neighboring frames support the March offset segment; the September reading does not gain independent temporal support.

Eight observed date digits can retain the fixed US month/day/year widths when OCR loses a separator, such as `0317/2026`. Seven ambiguous digits are not padded, and no date digit is supplied from metadata. The January Sheet 5 sample still has no valid date mapping, and the sampled Sheet 2 clocks still do not support one continuous offset. These failures remain visible.

The workbench's **Clock readings and layout checks** disclosure shows competing reads and proposed crop translations. The saved observations are versioned; a new reader never overwrites prior observations or human edits. Tesseract engine scores and phase-response/correlation values are not calibrated probabilities.

## Boundary proposal contract

`clock-board-boundary-links-v1` uses current-manifest-pinned observations with at least three distinct source times, and considers nonnegative recording gaps up to ten minutes with at most two minutes of boundary extrapolation. Same-source aliases and repeated OCR alternatives do not increase physical sample support. Posted rows need agreement at two distinct times per source. Matching board rows can suggest a continuation; changed/unreadable rows leave a nearby gap unresolved. This stage does not detect general overlaps or duplicate uploads yet.

Each proposal includes input source hashes, reader/model versions, conditional uncertainty, clock alternatives, supporting frame IDs, and YouTube links. `GET /api/catalog` and immutable JSON releases include `source_boundary_links`. Proposals create no games or accepted statistics.

**Compare recordings** opens both retained anchors. **Use these spans in a game draft** adds short evidence spans with partial coverage and unresolved membership; it preserves existing score edits and requires a separate save. Extending or joining canonical game boundaries still needs review. Independently verified real overlap/continuation labels, full-day reconstruction, and season-scale association accuracy remain release-gate work.

## Full grayscale line readings

`traditional-board-clock-v10` adds a grayscale line-mode date and time read while retaining the original thresholded and raw-line alternatives. The two date crops and every preprocessing/segmentation choice remain visible. This recovers the January 16 date where thresholding had collapsed digits and the trimmed raw-line read added a spurious leading digit. No title-based repair or digit padding is used.

On the 99 retained detailed frames, 97 now have at least one parseable clock candidate, compared with 90 under v8. Parseability is not accuracy: the January frame at 00:30:03 still yields an isolated incorrect time, which fails the consecutive-offset requirement; 00:30:09.5 remains unreadable. Four later frames from 00:30:15 through 00:30:34.5 support two offset hypotheses, preserving the unresolved AM/PM alternatives. These are development checks, not an independent OCR accuracy evaluation.

The first 45-second bounded pass completed 96 frames and reported three pending. A second pass resumed those three, preserving the previously completed observations.
