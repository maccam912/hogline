# Delivery-side hog-line comparison

Open `/games/GAME_ID/hog-rays`, or select **Compare direction at the delivery hog line** in a player report's delivery ray panel. Green dashed lines show a local tangent at the delivery hog; pink lines preserve the original whole-house fit. Both use the same original receiving-house image, so the broom reference is unchanged. The second panel retains an original crossing frame with native handle observations.

The measurement reuses `register_common_sheet` and `world` from the bird's-eye reconstruction. A three-second, 60-Hz source burst is located from the native delivery-house velocity. Small yellow components are associated within bounded longitudinal/lateral gates; ambiguous alternatives are omitted. Nearby matching granite edges provide a median pixel offset to approximate a ground anchor for the handle. This offset is an approximation, not a physical handle-height calibration. The exact native crop hash/PTS of the preview is checked against the retained observation before publishing.

A local quadratic fits lateral position against forward distance within one metre either side of the delivery hog. Its derivative at the hog gives the ray direction; it is continued straight from that point to the opposite tee. At least twelve observations, support on both sides, and no sampling gap over 0.2 seconds are required. Repeated residual filtering rejects outliers. Window changes to 0.7 and 0.85 metres expose sensitivity in the projected tee crossing. These are experimental estimates, not validated error bounds or broom accuracy scores. Nominal geometry, lens distortion, identity ambiguity, handle position and camera timing remain limitations.

Measurements and explicit unavailable reasons are cached in `data/hog-rays/`, keyed by the entire current source-pinned ray and method version. JSON and preview checksums are verified on reads. Changed current rays cannot reuse prior measurements. No reconstruction inputs, database shot assessments, reports or job states are rewritten. Each POST is bounded to one short local burst and makes no paid requests.

For a portable snapshot with embedded original images:

```sh
PYTHONPATH=. .venv/bin/python scripts/hog_ray_comparison.py GAME_ID --measure
```

The snapshot is `data/hog-rays/comparison.html`. Omit `--measure` to export cached results only. Restart an already-running server to load the new routes; the portable snapshot can be opened immediately without interrupting reconstruction jobs.

The first October 30 Koski–Dobbs pass produced 18 supported comparisons and 30 explicit unavailable crossings (occlusion, ambiguous handles, insufficient granite-edge matches, or incomplete hog coverage). Every player's first available native crossing/receiving overlay was visually inspected. These comparisons do not establish that hog tangents systematically lie closer to the called broom.
