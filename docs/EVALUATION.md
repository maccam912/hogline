# Labeling and detector evaluation

The workbench labels colored **handle centers in crop pixels**. These are not surveyed stone footprints, centimeter measurements, scores, or named identities.

## Label a retained view

1. Select an overhead view and choose **Label an overhead view**. The editor locks the source frame while a draft is open. All coordinates use the retained crop's original resolution, regardless of display size.
2. Click red, yellow, or unknown-color handle centers. Toggle a point's full/partial visibility by clicking its label chip; × removes it. Drag **Ignore region** over occluded or unobservable areas. Use **Mark non-stone area** over clearly visible clothing, equipment, or other known false objects. These pink regions override ignored regions: predictions inside them count as false positives and cannot match a nearby true handle. Points cannot also be inside either kind of region. Remove mode deletes a non-stone region before an overlapping ignored region.
3. Use **model candidates as a draft** only as a starting point. This marks the provenance `model_seeded` and the review status `draft`; such output cannot be evaluated as reviewed truth. Inspect every seed, remove false positives, and add missed visible handles.
4. State coverage separately from review. **All visible handles labeled** can be a valid empty-house negative example. Partial and unobservable views never supply a recall denominator.
5. Select the actual provenance: human reviewer, assistant visual audit, or known synthetic construction. Check the review box only after inspecting the view. A point/region edit clears that box. Save with an author and revision reason.

Keyboard modes outside input fields: **R** red, **Y** yellow, **U** color unknown, **I** ignored region, **N** non-stone area, **X** remove. Ctrl/Cmd-Z undoes a draft mark. Draft undo is separate from the durable revision history. Closing a dirty draft keeps an in-memory scratch copy until the page reloads; saving creates an immutable historical revision and optimistic checks reject stale edits.

## Freeze a cohort

**Evaluate CV** lists recording/day groups. Keep every frame/view from a recording, known duplicate-upload family, and recording day together. Default group suggestions use the title date as a conservative grouping hint; verify overnight games, changed titles, and continuation/duplicate pairs before freezing a real evaluation corpus.

Assign whole groups to train, validation, or test. The server rejects a recording or known title day spanning multiple partitions. A frozen dataset copies exact label revisions, source manifests, PTS/timebases, crop/image hashes, exclusions, and the partition specification. Later label edits create new revisions without changing existing datasets.

Confirmed mode includes complete, reviewed **human** labels only. Exploratory mode can also include assistant and synthetic labels, with provenance retained in the report. Model-seeded drafts remain ineligible. Synthetic checks test the implementation; they do not establish performance on Wausau footage.

## Evaluate and compare versions

Choose a frozen partition and detector version. Current-detector predictions can be generated from retained evidence without acquiring video. Historical versions use their retained observations; missing predictions/images are exclusions and reduce reported coverage rather than becoming invented misses or zeros.

Matching is one-to-one: maximize the number of valid matches, then minimize center error within the declared tolerance, expressed as a fraction of crop width. Reports preserve both color-agnostic detection and color-aware measurements. Predictions in ignored regions are excluded unless a known non-stone region also covers their center. Reports separately count `known_negative_predictions` among false positives. Older frozen datasets without `negative_regions` retain their original semantics; corrections require a new label revision and frozen dataset. Unknown-color labels do not enter color accuracy. Empty views with no predictions have undefined precision/recall rather than a fabricated perfect score.

Every report stores per-view rows and breakdowns by sheet, resolution, provenance, and evaluation group, along with exact dataset/model/observation references. Denominators and exclusions are visible. Adjacent frames and shared days are correlated; these descriptive reports do not yet establish the design's statistical release gates. They never authorize automatic score acceptance.

## CLI and API

```sh
uv run hogline labels
uv run hogline label-import /absolute/path/label-edit.json
uv run hogline freeze-dataset /absolute/path/dataset-request.json
uv run hogline evaluate DATASET_ID --partition validation --output /absolute/path/report.json
uv run hogline evaluate DATASET_ID --partition validation --model opencv-hsv-handles-v2
```

`label-import` takes a `LabelEdit` document (`expected_revision`, `author`, `reason`, and `labels`). `freeze-dataset` takes `name`, `author`, `reason`, `mode`, and a `partitions` map from group name to `train`, `validation`, or `test`. The schemas and routes are available at `/docs`.

Label revisions, frozen datasets, evaluation reports, original images, and all actual source metadata stay in the local ignored data directory. They are not committed to the public repository. JSON downloads contain provenance, coordinates, and evidence references; they do not embed video or image bytes.

## Initial development case

The Sheet 4 frame at [source 5:00](https://www.youtube.com/watch?v=es9urvYvLiE&t=300s) has an assistant-audited development label set: eight visible/partially visible handles and two ignored player occlusions. Seven model seeds were visually checked and the missing top-border red handle was added. A later revision marks that handle partially visible.

The frozen **validation** case reports seven matched handles, one miss, and no extras for `opencv-hsv-handles-v3`. Version 4 removes long straight colored border bands before contour extraction and recovers the clipped red handle: eight matches, no misses, no extras on the same case. Both versions' observations/reports remain available, and no video was reacquired to compare them.

This is a targeted fix on one development view with assistant provenance, not an independently labeled held-out accuracy result. It is deliberately ineligible for confirmed evaluation. The source corpus is also rerun across all eight sheets to surface changed candidates; unlabeled views are not assigned accuracy scores.

## Cross-sheet development audit

Thirteen assistant-audited views cover all eight sheets, two recording dates, and 320-/480-pixel house crops. They contain 19 visible handle labels and nine views with no visible handles outside occlusions. All groups are assigned to **train**, not held-out testing. Six detections on a foot, face, and red clothing were initially hidden by broad occlusion masks; explicit non-stone regions now count them correctly as errors.

The v4 baseline on this corrected frozen development cohort has **19 matches, 6 false positives, and 0 misses** (76% precision). Eight empty views are correctly empty; the ninth has the foreground false positive. These are small, selected development examples with assistant provenance, not release accuracy or independent ground truth. Existing earlier frozen datasets/reports remain unchanged. See [vision experiments](VISION_EXPERIMENTS.md) for the generic person-model trial and its limitations.


### Refined color baseline (v5)

`opencv-hsv-handles-v5` retains v4's raw contour generator, then rejects colored components below 0.035% of crop area (with a six-pixel floor) or circularity below 0.55. These Wausau seed-crop development priors remove tiny colored foreground fragments and irregular clothing patches. They may also reject genuinely small, blurred, occluded, or differently framed handles. They do not prove that a retained object is a stone. Rejected proposals retain original coordinates, shape measurements, and explicit reasons in both frame observations and detailed-window Parquet.

The unchanged 13-view training cohort improves from 19 matches / 6 extras / 0 misses in v4 to 19 / 0 / 0 in v5. Four additional active views at later moments were visually labeled before inspecting their detector predictions. The expanded **17-view, 39-handle** cohort reports 39 / 6 / 0 for v4 and 39 / 0 / 0 for v5; one additional v4 prediction is still in an unknown occluded area and excluded. These views share days with development data; neither result is held-out performance. The old frozen reports and v4 observations remain available for comparison.

All 83 retained detailed frames (166 overhead views) were reprocessed locally across all eight sheets, with no video acquisition. This broad rerun kept 155 candidates and retained 37 rejected proposals for audit; most views are unlabeled and receive no accuracy claim. Synthetic regressions verify that rejection reasons survive storage, color specks and irregular patches are filtered, normal and partly clipped handles survive, and the size prior scales to the 320-pixel crop.
