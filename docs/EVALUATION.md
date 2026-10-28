# Labeling and detector evaluation

The workbench labels colored **handle centers in crop pixels**. These are not surveyed stone footprints, centimeter measurements, scores, or named identities.

## Label a retained view

1. Select an overhead view and choose **Label an overhead view**. The editor locks the source frame while a draft is open. All coordinates use the retained crop's original resolution, regardless of display size.
2. Click red, yellow, or unknown-color handle centers. Toggle a point's full/partial visibility by clicking its label chip; × removes it. Drag **Ignore region** over occluded or unobservable areas. Points cannot also be inside ignored regions.
3. Use **model candidates as a draft** only as a starting point. This marks the provenance `model_seeded` and the review status `draft`; such output cannot be evaluated as reviewed truth. Inspect every seed, remove false positives, and add missed visible handles.
4. State coverage separately from review. **All visible handles labeled** can be a valid empty-house negative example. Partial and unobservable views never supply a recall denominator.
5. Select the actual provenance: human reviewer, assistant visual audit, or known synthetic construction. Check the review box only after inspecting the view. A point/region edit clears that box. Save with an author and revision reason.

Keyboard modes outside input fields: **R** red, **Y** yellow, **U** color unknown, **I** ignored region, **X** remove. Ctrl/Cmd-Z undoes a draft mark. Draft undo is separate from the durable revision history. Closing a dirty draft keeps an in-memory scratch copy until the page reloads; saving creates an immutable historical revision and optimistic checks reject stale edits.

## Freeze a cohort

**Evaluate CV** lists recording/day groups. Keep every frame/view from a recording, known duplicate-upload family, and recording day together. Default group suggestions use the title date as a conservative grouping hint; verify overnight games, changed titles, and continuation/duplicate pairs before freezing a real evaluation corpus.

Assign whole groups to train, validation, or test. The server rejects a recording or known title day spanning multiple partitions. A frozen dataset copies exact label revisions, source manifests, PTS/timebases, crop/image hashes, exclusions, and the partition specification. Later label edits create new revisions without changing existing datasets.

Confirmed mode includes complete, reviewed **human** labels only. Exploratory mode can also include assistant and synthetic labels, with provenance retained in the report. Model-seeded drafts remain ineligible. Synthetic checks test the implementation; they do not establish performance on Wausau footage.

## Evaluate and compare versions

Choose a frozen partition and detector version. Current-detector predictions can be generated from retained evidence without acquiring video. Historical versions use their retained observations; missing predictions/images are exclusions and reduce reported coverage rather than becoming invented misses or zeros.

Matching is one-to-one: maximize the number of valid matches, then minimize center error within the declared tolerance, expressed as a fraction of crop width. Reports preserve both color-agnostic detection and color-aware measurements. Predictions in ignored regions are excluded. Unknown-color labels do not enter color accuracy. Empty views with no predictions have undefined precision/recall rather than a fabricated perfect score.

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

The frozen **validation** case reports seven matched handles, one miss, and no extras for `opencv-hsv-handles-v3`. This is one development view with assistant provenance, not an independently labeled held-out accuracy result. It documents a reproducible failure to improve and is deliberately ineligible for confirmed evaluation.
