# Native delivery-house rays

The estimated player report now has a source-image overlay for every attributed delivery in the October 30 Koski–Dobbs Sheet 4 recording. Expand a shot to see its delivery-house handle track, native timing, and a simultaneous receiving-house image with a straight ray, tee crossing and sensitivity band. These remain assistant estimates. No strict shot, player identity or accuracy rating is accepted by this operation.

## Perspective overlays and paired distance check

All 48 reviewed deliveries now also have **left and right perspective overlays**. The same fitted straight ray is projected into each original camera crop with a dotted pink line, a tee crossing, a cyan tee line and a gold hog line. Both perspective backgrounds come from the same retained composite frame as the native delivery panel. Near/far coordinates and the right perspective's reversed transverse axis are explicit; behind-camera points and off-sheet continuation are clipped. These are approximate ice-plane projections, with extrapolation beyond the locally supported landmarks, not reconstructed curling trajectories.

`perception/perspective_rays.py` fits each local camera to the visible blue ring boundaries, the centre line and the nominal **21 ft / 6.4008 m tee-to-hog distance**. Every fifth ring-contour point is withheld from fitting. For this recording, held-out p90 contour residuals are approximately 0.83 px left and 0.87 px right. Pixel residuals do not establish world-position accuracy. The reviewed source-scoped seeds, handedness, media checksum and paired granite-edge coordinates are in `hogline/media/references/wausau-sheet4-oct30-perspectives.json`; another source or encode is rejected. These fits currently support this reviewed 1280×720 layout, not arbitrary perspective cameras.

The distance audit does **not** use 114 ft as a fitting constraint. Assistant-inspected frames at **512, 513, 514, 1332 and 1333 seconds** show the same isolated yellow stone in both perspectives, with both delivery directions represented. Each local map projects the camera-near bottom granite edge; one nominal 0.145 m radius per view is subtracted to approximate the stone centre. Summing the two local longitudinal distances gives a median **33.20 m**, with individual estimates **32.80–33.26 m**. Varying ring-interface placement from 0–1 px, hog placement by ±1 px and granite-edge placement by ±1 px independently in both cameras gives **30.57–35.75 m**. This is a deterministic sensitivity envelope, not a confidence interval and not a bound on all physical errors.

**Retain 114 ft / 34.7472 m.** It lies within that sensitivity envelope, but these shallow views neither independently confirm a surveyed button distance nor justify replacing it with the 33.20 m point estimate. Lens distortion, extrapolation beyond the hog line, ground-point placement and possible camera timing offsets remain unmodeled. A shared broadcast PTS does not prove synchronized camera exposures. The report exposes this result, a paired marked image and links to all five original composite frames.

The existing native source-frame house timings are now explicitly displayed as a **delivery weight proxy**, with elapsed seconds and nominal average speed: shorter split means faster passage. Forty-five deliveries have full-house estimates and three retain partial timing. The perspective panels repeat the same timing evidence; they do not pretend to provide an independent split measurement. Hand-controlled passage is not release speed or a stopping-distance forecast, and timings in different directions or ice conditions need context.

`POST /api/games/{game_id}/skill-estimates/perspective-rays` takes `candidate_id` and optional `calibration_id` (default `wausau-sheet4-oct30-perspectives`). Construct the native ray first. The extension creates a new immutable `SkillDeliveryRay`, preserving its base ray and report revision, and pins the shared reference and five paired frames. Deduplicated evidence IDs are resolved before publication. Repeat requests reuse matching current extensions. The existing SVG endpoint additionally accepts `perspective_left`, `perspective_right` and `distance`; changed/corrupt paired evidence withholds the extension instead of silently showing the older ray.

Implementation: `perception/perspective_rays.py`, `orchestration/perspective_rays.py`, `analytics/perspective_rays.py`, report panels and the review routes. Recovery backup, receipts, distance diagnostics, source-frame previews and render review artifacts live in ignored `data/perspective-rays/` and need separate backup.

Validation: 728 local tests passed; focused Ruff/format/diff checks passed. All 240 live SVG endpoints returned 200 and parsed, all 96 perspective overlays rasterized, and every player's twelve-shot perspective mosaic plus the paired-stone image was visually reviewed. Database integrity/foreign keys passed; original report revisions, batch hashes, job states and paid-vision ledger were unchanged. Cache usage was 1160.33 MiB under the unchanged 2048 MiB cap. Full browser-layout review was unavailable because local browser interaction is disabled in Work settings.

## Distance and geometry

[World Curling's October 2026 rules](https://worldcurling.org/wp-content/uploads/2026/10/Rules-of-Curling-2026.pdf), R1, place the tee lines 57 feet either side of the sheet midpoint: **114 feet / 34.7472 metres between buttons**. The nominal house radius is six feet and the hack line is twelve feet behind the tee. No independent Wausau sheet survey was found; the app explicitly uses nominal dimensions, not a claimed venue measurement.

`perception/delivery_ray.py` maps the painted rings into an approximate ice plane using the existing projective-house diagnostics. Its separate approximation gate requires two outer boundaries with at least 30 of 36 angular sectors, held-out inlier fraction >= 0.7 and inlier p90 residual <= 0.012 house radii. It does not relax the strict three-ring calibration gate used by other measurements. A visible centre-line fit establishes the longitudinal axis when supported; any image-vertical fallback is recorded. Ring residuals do not establish physical accuracy. Lens distortion, handle height, paint tolerances and camera movement remain uncalibrated.

For this recording, both overhead views have the same image-X handedness. Odd estimated ends deliver from `house_bottom` towards decreasing image Y; even ends deliver from `house_top` towards increasing Y. These were visually checked for this recording; end parity is not a universal direction rule. Production requests take explicit origin view and travel sign. The fixed-camera transfer remains a recorded assumption, requiring review for another camera arrangement.

With forward distance `s = forward_sign * origin_y`, fit `x = a + b*s`. The opposite tee crossing is `a + b*34.7472`. At the receiving camera, longitudinal coordinate `y` maps to `s = 34.7472 + forward_sign*y`; this preserves X and the true fitted angle. The ray's intercept at the nominal near hack is also retained (`a - b*3.6576`); the fit is not forced through an invented central hack point. This is a straight geometric continuation, without curl or subsequent steering.

## Native acquisition and timing

`media/windows.py::decode_native_crop` selects original source frames, preserving FFmpeg PTS and time base. It does not interpolate, synthesize or retime frames. A decode is limited to 720 samples / 30 seconds; 60 Hz native bursts are limited to twelve seconds. The current orchestrator accepts the verified 1280x720, 60 Hz Wausau composite and refuses nonzero source timeline starts. Native trajectory intervals must lie on its source frame grid. Sixty-Hz source presentation times do not prove a sixty-Hz original camera exposure cadence or physical timing accuracy.

`orchestration/delivery_rays.py` first searches a bounded 24-second interval at five samples per second, since a candidate timestamp may show setup rather than the actual pass. It then decodes a short native burst. Default HSV proposals link yellow handles by direction and motion. The optional leading-yellow-edge detector handles sleeves joined to handles; it can also detect clothing and needs visual review. Camera-border detections are excluded. This is not an object identity classifier.

The house split uses the two longitudinal planes six feet either side of the tee. An observed crossing retains both native PTS brackets. A missing edge can be estimated only within **0.35 metres and 0.15 seconds**, from the nearest twelve observations; it retains its actual anchor PTS and is labeled extrapolated, without an invented source frame. Full splits involving either such edge are labeled estimates. A larger missing segment remains a partial pass with its actual observed span and elapsed frames, not a full-house time. Count differences are source presentation-frame intervals, not the count of unique detected stone poses.

The ray sensitivity combines a pixel perturbation assumption (1.5 pixels by default, three for the sleeve detector), longitudinal orientation sensitivity, and early-versus-late half-fit projections. It is not a statistical confidence interval and cannot cover all camera/model errors. A wider or unstable track can be withheld; approximate geometry never becomes confirmed calibration.

## Provenance and API

`POST /api/games/{game_id}/skill-estimates/delivery-rays` accepts:

```json
{
  "candidate_id": "an attributed yellow delivery ID",
  "origin_view": "house_bottom",
  "forward_sign": -1,
  "reference_seconds": 300,
  "detector": "yellow_handle"
}
```

Use `yellow_leading_edge` only after checking the proposals against the stone. The request is synchronous and bounded to one delivery. It requires a current non-stale estimated report and verified local media; it makes no paid model calls. Existing matching current results are reused. Discovery and fitting happen outside the write transaction.

Publication creates an immutable `SkillDeliveryRay` with source/report/media pins, camera reference, geometry diagnostics, native pixel observations with PTS/time base/crop hashes, detector choices and interpretation. Selected first/middle/last and observed crossing frames are retained as native full parents with regenerable crop descriptors; their pixel hashes must match the native crop decode exactly. Evidence deduplication resolves IDs before checksum pinning. Retention protects the pinned parents. Other native crops can be regenerated from the checksum-verified original recording; the private burst JSON cache is an optimization, not an API input or new source of truth.

Current reports attach only the latest ray per candidate with matching report/source/evidence/image checksums. A corrupt newest proposal does not silently fall back to an older ray. Earlier immutable proposals remain stored. SVG endpoints embed the actual checksum-checked crop, avoiding external-image loading failures when the SVG is used as an image. Off-image tee crossings have an edge marker instead of an invented in-house dot.

## Weight interpretation and remaining automation

A house split measures speed **during delivery**, often while the hand is still on the stone. It cannot alone predict whether the stone will stop before or beyond the other tee: subsequent push, release, ice, curl, sweeping and contact matter. The app exposes the split and labels supported existing front-of-rings outcomes as **observed light of tee**, including that they may be intended guards. It does not present these outcomes as forecasts. Heavy/light prediction is explicitly unavailable; no validated stopping-distance model has been fitted.

An exploratory check on this recording also failed to justify a forecast: among 27 reviewed finishes with full-house timing estimates, the thirteen front-of-rings placements had median 1.386 seconds and the fourteen inside-rings placements had median 1.383 seconds. A monotone timing threshold trained separately while leaving each delivery out classified only 13/27 correctly, versus 14/27 for the majority baseline. These are assistant-reviewed front/inside groups, **not** tee-side ground truth, controlled calls or an independent benchmark. They demonstrate why a simple timing-based light/heavy label was withheld here; they do not prove no richer model could work. Inputs and results are retained in `data/delivery-rays/weight-sanity-check.json`.

The next automation milestone should collect a distinct release/free-running segment, hog-line or other timed planes, received stone motion and a stable final position. Separate contact/cleanup cases and sweeping context. Fit a recording/ice/direction-aware stopping-distance model with uncertainty, and evaluate held-out deliveries before enabling a predictive light/heavy label. LLM review can identify the release, ambiguous handle, broom purpose and shot phase from a bounded native evidence pack; it must retain timestamped anchors and unknowns. It must not invent the missing speed-to-stop calibration. See [the report automation plan](SKILL_REPORT_AUTOMATION_PLAN.md) for the broader pipeline.

## October 31 checkpoint

All four players have twelve projected rays. Forty-five of 48 deliveries have crop-edge-estimated full-house times; three Matt deliveries (candidate setup times 32:10, 44:36 and 62:02) retain partial-house timing because the handle becomes obscured too far from the missing edge. Origin-track mosaics for every player were visually inspected. The browser report was checked on Matt's first delivery, including the original receiving image, tee dot, angled ray and visible broom. No Wausau survey, released aim, calibrated shot success or stopping-distance forecast is claimed.

Ignored `data/delivery-rays/` contains the recovery database, native crop caches, per-point hashes, immutable publication receipts, report exports, contact sheets and live-validation receipt. Its data must be backed up separately from Git. No extra paid model requests were made.

Local validation: **719 tests passed**, including native sixty-Hz PTS/pixel identity, invalid cadence, both travel directions, projective mapping round trips, true receiving-ray angle, bounded edge estimates, partial passes, provenance, retention and corrupt-latest withholding. Focused Ruff and diff checks passed. The live uncached API reproduced Ethan's first pass, then reused its immutable result on repeat; all **96 SVG endpoints and 168 retained source-frame endpoints** passed. The original player review remains revision 2, all three prior completed batch documents retain their hashes, and database integrity/foreign keys passed. Evidence cache measured **1156.74 MiB** under the unchanged **2048 MiB** cap. Browser rendering was verified on the actual player report. No commit or push was requested.
