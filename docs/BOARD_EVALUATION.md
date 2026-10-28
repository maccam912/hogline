# Traditional-board benchmark

Select a retained detailed frame and expand **Clock & scoreboard observations → Label visible board cards**. The original frame, retained board crop when available, and source timestamp link remain beside the form. For each row, either mark it unreadable/obscured or enter every visible movable end number under its cumulative point column. A readable empty row is a negative example. Storage markers and blank-end conventions are outside this benchmark. An obscured row does not imply an empty row.

Labels are separate from scores and game interpretations. Saving uses an expected revision and records reviewer, reason, provenance, image checksum, and immutable history. Original images must still exist and match their checksums. New labels start as drafts with both rows unreadable. Model predictions are never copied into the form automatically. Unverified model seeds cannot be marked reviewed. Assistant visual inspection is explicitly distinct from human inspection.

Open **Board benchmark** to assign whole groups to training/development, validation, or test and freeze a dataset. All labels from the same recording or known title date must stay in one partition, even if they have different group names or sheet numbers. Review midnight games and duplicate-upload families yourself; title dates alone cannot establish those relationships. Human-only mode excludes assistant, synthetic, and model-seeded labels. Exploratory mode can include reviewed assistant and synthetic examples. Each requested group must have eligible labels.

Datasets retain exact label revisions, image manifests, source context, exclusions, and a checksum. Editing a label later does not change an existing dataset. Evaluating a stored broadcast reader does not download media or run OCR; run **Read cached frames** first when a new reader version needs predictions. Results retain the exact predictions, model version, dataset checksum, partition, label provenance, and per-row source timestamps/YouTube links.

Metrics deliberately keep difficult examples:

- Card precision/recall use exact `(end number, cumulative column)` matches within a color row. A wrong numeral or column is an extra prediction and a missed label. Duplicate predictions cannot inflate matches.
- Exact-row accuracy requires a complete reading with precisely the labeled card multiset. An unprocessed or abstaining reader does not receive credit for an empty row.
- Missing prediction frames remain in the denominator, with labeled cards counted as misses.
- Unreadable labeled rows are excluded from card accuracy and reported separately, including how often the reader nevertheless returned a complete reading. This identifies a visibility disagreement; it does not establish which hidden cards existed.
- Totals and per-sheet results are shown separately. These are frame/row checks, not independent game-level accuracy or end-score accuracy. Repeated nearby frames are correlated.

JSON downloads preserve both datasets and results. The API is `/api/board-labels/{evidence_id}` (GET/PUT), `/api/board-evaluation` (GET), `/api/board-evaluation/datasets` and `/runs` (POST), and `/api/board-evaluation/{datasets|runs}/{id}.json` (GET). CLI equivalents:

```sh
uv run hogline label-board EVIDENCE_ID board-label-edit.json
uv run hogline freeze-board-dataset dataset-request.json
uv run hogline evaluate-board evaluation-request.json
uv run hogline board-benchmark
```

The JSON request types are `BoardLabelEdit`, `DatasetCreate`, and `BoardEvaluationRequest` in the API documentation. Labels, images, datasets, and reports belong under ignored `data/`; no real media or participant labels are published with the source code.

## Initial development check

Twenty-one assistant-inspected retained frames cover all eight sheets and January 16/March 17, including empty boards, visible posted cards, seven obscured rows, and three automatic-registration failures. The March frames were selected near 300 and 5400 seconds per sheet; the remaining frames cover the earlier January check and known Sheet 4 refinement cases. Both dates are assigned to **training/development** because they have already informed implementation. This is not a held-out accuracy estimate.

Reader `traditional-board-clock-v10:f6c3999acd8b0f48` matched 22 cards, missed seven, and produced no extra cards on the 35 readable labeled rows. It returned exact complete readings for 29/35 rows; six readable rows abstained. Three of seven obscured labeled rows received complete readings. The per-sheet report exposes failures on sheets 3, 4, 5, and 6 that an empty-board-heavy aggregate could hide. The UI label-save, freeze, evaluate, and download routes were checked, and tests cover revision conflicts, frozen history, source/day leakage, missing predictions, corrupt images/datasets, duplicates, and nonhuman-label exclusions.

Reader v11 adds conservative per-cell visibility checks: a white card candidate must span at least 65% of its row height and have white support in its upper 30%; otherwise it remains unreadable. Cells without a white candidate also need at least 65% expected red/yellow background to avoid an obstruction flag. These are image heuristics, not calibrated probabilities or a person detector. Diagnostic fractions and reasons are retained. On the same frozen development labels, v11 kept 22 card matches, zero extras, and seven misses, improved exact rows to 30/35, and reduced complete readings on the seven obscured labeled rows from three to zero. At the known Sheet 4 white-hat frame, both false yellow numeral candidates were rejected before OCR while the red row stayed readable. This is a development regression check; a new held-out set remains necessary.
