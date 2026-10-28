# Automatic delivery evidence and release associations

New detailed windows now acquire body-limb cues and perspective colored-handle proposals alongside anonymous person fragments. The goal is unattended delivery/thrower association and named timing, with review concentrated on uncertain cases. These are intermediate observations: an inferred release is not yet an accepted played shot, named player or weight measurement.

## Models and retained inputs

The optional [OpenCV Zoo MediaPipe pose model](https://github.com/opencv/opencv_zoo/tree/48b10fa505aeb51fbd127fc60a5c660040e823b7/models/pose_estimation_mediapipe) is pinned to the linked commit, 5,557,238 bytes and SHA-256 `9d89c599319a18fb7d2e28451a883476164543182bafca5f09eb2cf767ed2f3f`. Its directory is Apache-2.0 licensed. Keep the weights at `DATA_DIR/models/mediapipe-pose/pose_estimation_mediapipe_2023mar.onnx` or set `HOGLINE_POSE_MODEL`. The local development archive already has the model; weights are ignored by Git.

Each uncut person box produces a square image-space crop enlarged by 1.3, resized to 256×256 RGB, with pixel values scaled to 0–1. This adapts the model to the person detector's boxes and has not been validated as a curling pose estimator. Weak or misaligned results abstain. At most twelve people per perspective sample receive pose inference; others have explicit missing reasons. Budget/cancellation checks run before each inference. Missing or failed pose inference leaves person observations available.

Only limb/torso/foot landmarks 11–32 are retained, with original crop-pixel coordinates, raw visibility/presence, and inside-crop flags. No face landmarks, biometric naming, world-space joint coordinates, or model-derived physical lengths are stored. Model scores are uncalibrated.

Perspective handle proposals retain color centers, bounding boxes, shape and gray-neighborhood cues. The seed wall/logo and clock strips are excluded. Person-overlapping candidates remain visible in the raw data; tracking excludes upper-body overlaps unless a sufficiently visible wrist is nearby. These are object hypotheses, not verified granite boundaries. Brooms, clothing and other small colored objects remain failure cases. This is a concrete reason to train a club-specific stone model.

The v2 perspective Parquet includes these features and pins pose/handle methods and model hashes. V1 person-only artifacts remain readable; they do not gain missing pose or handle evidence from a cached rerun. Person fragments now retain the original per-row person index, so release hypotheses refer to the same detector instance rather than joining unrelated body boxes.

## Conservative release hypotheses

The release matcher requires a consistent visible wrist near an object across multiple samples, an observed stationary control prelude, subsequent coupled movement, and continued object travel after observed separation from that same actor's hand. At least three samples across half a second support control and separation. Version `perspective-release-associations-v2` measures the stationary prelude over elapsed time rather than the first three samples, so 10/30-Hz refinement preserves the same physical-duration requirement. Incomplete hypotheses retain up to four original-timestamp hand-control intervals for bounded refinement. Competing hands, broken actor continuity, missing control and missing separation remain explicit uncertainty reasons. Views remain separate: two camera proposals do not become two counted physical deliveries.

Results preserve the last controlled and first separated original PTS/timebase, source links, raw object/actor references and uncertainty intervals. Object class, sheet/end phase, named actor, calibrated timing and league/shot context remain unresolved. Neither a bent player nor a moving handle alone supplies a thrower or weight statistic. No end number, accepted shot count, player name or physical timing is invented.

Every new window derives this stage automatically under its existing budget. `hogline derive-deliveries WINDOW_ID` / `POST /api/windows/{id}/deliveries` recomputes it from cached perspective observations without loading models or fetching video. Catalog summaries, `GET /api/window-deliveries/ID` and immutable JSON releases retain exact input and parent actor-result checksums. Results are bounded to two MiB; an oversized result abstains without discarding the raw observations.

## Development checkpoint, October 28, 2026

A real Sheet 3 interval at source 01:29:40–01:30:10 ran the decoder, person model, pose model and perspective handle extraction within a 32-MiB/180-second allowance. It retained 17.27 MiB in 34.18 measured worker seconds, 787 observed-pose proposals, forty anonymous body fragments and ten moving handle fragments. Those counts describe experimental outputs, not measured detection precision or unique people/shots.

The release matcher abstained on all ten moving fragments: seven lacked persistent visible hand control, two lacked coupled movement before separation, and one lacked an observed stationary control prelude. This is a retained failure case, not a successful automatic thrower association. Small/occluded stone observations and pose ambiguity still interrupt control evidence. The next useful work is a corrected club-specific object corpus and trained detector, then delivery/end evaluation on independently labeled games.

Controlled fixtures exercise a positive stationary-control → coupled-motion → separation sequence through both perspective views, exact PTS and columnar round-tripping, missing/competing hands, missing preludes, actor gaps, output-size limits, cached derivation and release-lineage failures. They verify contracts and abstention behavior, not real-footage accuracy.


Automatic campaigns can now select a single additional refinement stage around supported brackets, within the existing cumulative budget. A real Sheet 3 bracket selected source 5392.667–5396.667 seconds: 120 native samples per view, 8,989,084 retained evidence bytes and 32.42 worker seconds. The denser run still found no supported release and recorded one moving fragment without persistent hand control. This preserves a useful high-frequency failure case for a better object/pose model; it does not validate automatic thrower attribution. The initial attempt exposed an eight-frame retention assumption and failed without publishing a window; the corrected per-view start-anchor implementation completed a fresh pinned run.
