# Local handle-classifier experiments

Hogline can train a small CPU OpenCV random forest from frozen, independently reviewed overhead handle-center labels. This is a cheap first experiment in learning which colored proposals are handles. It is not a granite-footprint detector and cannot recover objects that the raw color proposer misses. No model is downloaded or activated by training.

Freeze an exploratory or confirmed dataset with whole recording/day groups assigned to `train`. Label complete visible views, with true handle centers, occlusion regions and known non-stone regions. `model_seeded` labels and partial views cannot train a model. Unmatched raw proposals in complete reviewed views supply negative examples; explicit negative regions override occlusions.

Create a request file in ignored local storage:

```json
{
  "dataset_id": "FROZEN-DATASET-ID",
  "author": "Operator",
  "reason": "Learn handle context from corrected development views",
  "seed": 17,
  "trees": 96,
  "max_depth": 8,
  "vote_threshold": 0.8,
  "max_examples": 4096,
  "max_worker_seconds": 120
}
```

Run `uv run hogline train-handles data/training-request.json`. The equivalent API is `POST /api/handle-models/train`. Training reads at most 256 frozen train views, caps the augmented feature matrix, and checks a cooperative time budget. OpenCV's bounded forest call is checked on return, rather than forcibly interrupted. Original and horizontally flipped context patches supply HOG, HSV histogram and low-resolution color features. Ground-truth centers and negative proposals retain their exact label revision, evidence checksum and provenance.

Weights stay under ignored `data/models/trained-handles/` with a SHA-256 filename. The database pins the weights, feature and raw proposer versions, threshold, OpenCV version, dataset hash, training groups, sources and all known title days. `GET /api/handle-models` lists manifests; `GET /api/handle-models/runs/ID.json` exports a checksummed run. Neither public Git pushes nor catalog-only exports back up the local weights.

Select the registered model version in **Evaluate CV**, or run:

```sh
uv run hogline evaluate DATASET-ID --partition test --model opencv-handle-forest-v1:MODEL-DIGEST
```

Missing cached predictions are computed from retained crops. Validation/test rejects any source, title day or evaluation group overlapping the training cohort, including historical metadata aliases. A separate dataset ID does not make training footage independent. `--partition train` permits a clearly marked development check. Corrupt manifests or weights fail before cached predictions can be reused.

Tree-vote fractions are uncalibrated and do not establish probability. Training fit, even perfect fit, does not grant automatic score or shot acceptance. Compare precision, recall, color errors and coverage on independently labeled, disjoint days with people, clutter, obstruction, both house ends and representative sheets. Add a trained instance/segmentation model and physical granite labels when color-proposal recall or stone geometry becomes the limiting factor.

## First real experiment, October 28, 2026

The local development cohort had 19 reviewed views, 45 labeled handles and eight negative proposals on January 16 and March 17. Ninety-six trees produced a 132 KiB classifier. The original development views returned 45 matches, no extras and no misses. These are **training-day results**, not independent accuracy or a release gate. Previously cached March 7/8 samples were empty or too dark for a useful positive held-out benchmark, so they do not validate the classifier. The default HSV detector remains unchanged.
