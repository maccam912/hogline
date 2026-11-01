# One-shot top-down reconstruction

This is a **one-off, assistant-reviewed demonstration**, not an automatic reconstruction campaign or a shot-accuracy rating.

Open **http://127.0.0.1:8765/examples/shot-reconstruction/**. The viewer has the reconstructed video, native-cadence original clip, playback speed controls, downloads and timestamped observation/trajectory data. The source is the existing Oct. 30 Sheet 4 Koski–Dobbs recording, Lindsey's first yellow delivery in estimated end 3 (`ac2a4232-fe05-5529-a6b4-537a2b47d03a`). The original recording interval is **2118–2158 seconds / 35:18–35:58**. It includes setup, delivery, transit, arrival and several seconds with the final yellow stone visibly at rest.

## Outputs

Ignored directory: `data/shot-reconstruction/example/`.

- `source-shot.mp4`: 40.000 s, 1280×720 H.264, 60 Hz source presentation cadence, 2,400 frames. Re-encoded from the checksum-verified parent; no audio track exists in the original.
- `reconstruction.mp4`: 40.000 s, 1600×1080 H.264, 24 fps, 960 frames. Original camera composite, blended four-camera top-down video warp, a clean 2D stone replay and a horizontal full-path view are synchronized in one movie.
- `index.html`: local video viewer with normal/half/double-speed playback and source download.
- `poster.png`, `preview-*.png`, `render-review.jpg`, `tracking-review.jpg`: visual checks, not new analysis acceptance.
- `observations.json`: 271 camera position observations with original source PTS/time base, camera pixels, crop hashes, world estimates, method and assumed fitting-error scales.
- `geometry.json`, `trajectory.json`, `manifest.json`: camera registration, fitted trajectory, source/review/ray pins, media/artifact checksums, nominal dimension assumption and limitations.
- `validation.json`, `http-validation.json`, `holdout-validation.json`: artifact, playback-range, state-preservation and bounded holdout checks.

The video shows **teal for camera-supported portions** and **amber dashes for unmeasured/occluded portions**. The yellow marker is reconstructed, including during occlusion. It is not an extracted hidden-stone image. The camera warp is a flat-ice mapping: raised people and other nonplanar objects stretch, and empty reference ice only supplies background where a view is missing.

## Reconstruction method

`scripts/shot_reconstruction_example.py` reads the current non-stale player review and pinned ray, verifies the parent media checksum and works locally without writing new database analysis results or making model calls.

1. Register the existing local perspective maps into a common sheet using five already reviewed paired-stone frames from **other shots**. This stage **assumes 114 ft / 34.7472 m** between buttons. It does not replace or independently confirm the earlier approximate distance audit. The original distance audit and published rays remain unchanged.
2. Reuse 92 original native delivery-house handle positions. Add 14 assistant-reviewed bottom granite-edge points and 90 bounded yellow-handle/gray-granite-body proposals from the perspective views. In these low-angle views, use the ground-edge cue with an approximate radius correction rather than directly treating an elevated handle as an ice point.
3. Add 75 isolated receiving-house handle observations, including the stationary finish. Additional sampling uses 320 original presentation frames at eight samples per second. Per-observation PTS/time bases and native crop hashes remain in the data.
4. Fit the whole shot simultaneously on a quarter-second state grid. Weighted camera position factors, acceleration/jerk regularization, nonnegative forward increments and setup/rest anchors constrain the path. Later observations constrain earlier missing states. Fitting weights and smoothing strengths are assumptions, not calibrated probabilities.
5. Reproject original clip imagery with precomputed planar maps and blend it into the common sheet, using the overhead views near the houses and perspectives in transit. Render the same fitted path over that video warp and a clean nominal sheet. The visualization export samples footage to 24 fps; measurement timing stays on original source PTS.

The longest unmeasured interval is **2140.000–2148.633 s / 8.633 s**, while sweeping people obscure the useful stone view before it appears in the receiving overhead crop. The interpolated bend and timing during that interval are one plausible constrained reconstruction. They are not uniquely recoverable from the recording. Camera exposure synchronization, lens distortion, handle/stone height and world-position accuracy remain uncalibrated.

The end position in this nominal coordinate system is approximately **+0.33 m transverse / 33.32 m forward of the delivery button**. This describes the reconstruction, not a surveyed measurement or execution error against an unknown call.

## Reproduce

Run from the repository root with the original recording and reviewed database present:

```sh
uv run python scripts/shot_reconstruction_example.py prepare
uv run python scripts/shot_reconstruction_example.py render
```

The script is intentionally scoped to this source, candidate, fixed layout and reviewed pixel anchors. Its outputs remain private/ignored and require separate backup. The read-only viewer routes only allow the named example files, support MP4 byte ranges, and reject unknown paths or symlinks escaping the artifact directory. No arbitrary data-directory file is served.

## Validation and limits

Both videos fully decoded to their expected frame counts and durations. Artifact checksums, viewer JavaScript syntax, all six live asset responses and MP4 HTTP 206 range requests passed. The setup, delivery, view handoff, long gap, arrival and resting finish were visually inspected using rendered frames and the eight-frame contact sheet. Full browser interaction remains unverified because Work settings disable local browser access.

The full local suite passed **730 tests**. Focused Ruff/format and diff checks passed. New route regressions cover the file whitelist, missing artifacts, video ranges and escaping symlinks.

A bounded check withheld all 21 position anchors from **2129.000–2131.500 s** and refitted the shot. Median agreement with those withheld approximate anchors was **0.034 m**, maximum **0.107 m**. This is not independent physical ground truth, a survey-quality accuracy claim or validation of the longer fully occluded interval.

Original review revisions, processing-batch hashes and job states were preserved. Database integrity/foreign keys passed; evidence cache remained **1160.33 MiB / 2048 MiB**. No recording-wide campaign, strict shot acceptance, identity changes or paid vision calls were initiated.
