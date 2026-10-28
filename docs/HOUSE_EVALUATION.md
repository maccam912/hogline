# House-state interval benchmark

House-state labels describe **visible handle configurations in a particular overhead crop**, independently of detector output. They do not label granite footprints, eligible scoring stones, physical A/B ends, official end numbers, or played results. The benchmark checks stable-state coverage and unsafe stability claims; transition-boundary evaluation remains future work.

## Annotate original intervals

Choose **Label top interval** or **Label bottom interval** on a detailed window. The form starts with unknown labels, keeps retained frames separate from source-interval inspection, exposes original sample timestamps, and saves revision history. **House benchmark** opens group assignments, cohort freezing, cached evaluation and report downloads. The default cohort mode admits reviewed human source intervals; use exploratory mode explicitly for synthetic or assistant inspection.

`GET /api/house-labels/WINDOW_ID/house_top` (or `house_bottom`) returns original sample indices/timestamps, retained context references, saved labels, and revision history. It supplies no detector predictions. Inspect the source interval and label inclusive sample ranges as:

- `stable_handles`: an unchanged, observable configuration, with independently inspected red/yellow visible-handle counts.
- `stable_no_handles`: observable and unchanged, with zero visible handles. This is not proof of an empty house.
- `changing`: the configuration or relevant view is changing.
- `unobservable`: obstruction or image quality prevents assessing stability.
- `unknown`: deliberately unresolved; uncovered samples also remain unknown.

Ranges may leave gaps but cannot overlap. Use unknown/unobservable where counts or stability cannot be assessed. Label all visible handles in the **whole crop**, including parked stones; this is not a house-eligibility test. Context anchors alone cannot establish what happened between them. `inspection_basis=retained_frames_only` labels are preserved but excluded from interval datasets. Review of the actual source interval uses `source_interval`; synthetic fixtures must declare both synthetic provenance and inspection basis. Model seeds cannot become reviewed ground truth.

Save a `HouseLabelEdit` JSON file with `hogline label-house WINDOW_ID house_top edit.json`, or `PUT /api/house-labels/WINDOW_ID/house_top`. Example:

```json
{
  "expected_revision": 0,
  "author": "Reviewer",
  "reason": "Inspected the original bounded source interval",
  "evaluation_group": "wausau-2026-03-17",
  "provenance": "human",
  "inspection_basis": "source_interval",
  "review_status": "reviewed",
  "segments": [
    {"first_sample": 0, "last_sample": 9, "state": "stable_handles", "red_handles": 2, "yellow_handles": 1}
  ]
}
```

Saving pins the dense artifact checksum and exact endpoint PTS/timebase/links. Author, reason, prior documents and revision numbers remain in history; stale revisions fail rather than overwrite another review.

## Freeze and evaluate

`hogline freeze-house-dataset request.json` / `POST /api/house-evaluation/datasets` accepts the shared dataset contract: name, author, reason, `confirmed` or `exploratory` mode, and a mapping of evaluation-group names to `train`, `validation`, or `test`. Confirmed mode admits reviewed human interval labels only. Exploratory mode can include explicitly identified assistant or synthetic labels.

Whole recordings and known title days stay in one partition. Both the pinned acquisition title day and the current known title day participate in leakage checks; refreshing metadata cannot silently move correlated examples into another partition. Explicitly group midnight continuations and duplicate upload families before freezing. Overlapping labels must agree at the same source time/view. Cohorts are bounded to 128 window/view records and sixteen MiB. The frozen artifact retains exact labels, revision authors/reasons, original metadata snapshots, windows, exclusions, and input checksums.

`hogline evaluate-house request.json` / `POST /api/house-evaluation/runs` accepts `dataset_id`, `partition` (default `test`), and `model_version` (default the current house-state version). Evaluation reads checksum-verified cached observations only. Current-method results can be derived locally; older requested results must already exist for every frozen window. Missing/corrupt inputs, changed window records, and result-limit abstentions fail explicitly rather than becoming a misleading zero-output report.

Each report separates evaluated samples, stable truth, stable predictions, exact color-count agreement, incorrect/unsafe stability, abstentions, unknown/unlabeled samples, and duplicate exclusions. Precision uses all stable predictions; recall uses stable truth; coverage uses all evaluated samples. Empty denominators are null. The confusion matrix and sheet/provenance breakdowns retain the sample counts. Overlapping source-time/view samples count once, using deterministic frozen-record order; repeated samples and adjacent frames remain correlated even after this suppression.

These are descriptive sample-level checks. Count agreement does not validate the predicted handle positions or a geometric score. A held-out independent corpus and separate change/end-boundary labels are still required before automatic end reconstruction. No score-acceptance gate is enabled by these reports.

`GET /api/house-evaluation` lists labels, datasets and reports. Full immutable artifacts are available at `/api/house-evaluation/datasets/ID.json` and `/api/house-evaluation/runs/ID.json` with verified checksums. Keep the local catalog and dense/evidence files together; these data are not committed to the public repository.

The save → freeze → evaluate workflow was exercised in the browser against an isolated synthetic local-video catalog. Its 28 changing samples produced 28 abstentions, zero stable predictions, null precision/recall denominators and 0% prediction coverage. The fixture and reports remain under ignored local data; this is a UI/pipeline check, not real-footage accuracy.
