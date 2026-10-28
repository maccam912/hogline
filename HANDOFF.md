# Hogline handoff

Checkpoint: October 28, 2026. The implementation is a working local pilot, not the completed system in [DESIGN.md](DESIGN.md). Version **0.30.1**, implementation commit `2d3f75e`, has 303 passing local tests, clean Ruff checks, checked browser scripts and a successful wheel/sdist build. Linux CI also passed that implementation commit. Check CI and current Git state again before continuing.

## Current instructions and priorities

The user wants continued development according to the design, with frequent commits and pushes. Prioritize unattended scores, associating stones with throwers, and per-player weight consistency, leaving human review for uncertain cases. Names are optional: persistent anonymous groups can be named or linked later by the operator. The implemented identity workflow uses body continuity and reviewed links, without face recognition. The user is open to training club-specific CV models; an experimental local classifier and leakage-safe benchmarks now exist.

Stop work if weekly remaining usage reaches **50% or less**. The last observed reading during this checkpoint was 6% used / 94% remaining; the threshold had not been reached. Recheck periodically. Do not treat this checkpoint as completion of the overall design.

## Starting a new session

Read [README.md](README.md) and the relevant design sections, then check branch, origin, worktree and CI. The shared checkout is `/Users/maccam912/dev/hogline`, `main`, origin `https://github.com/maccam912/hogline.git`. Preserve unrelated changes and existing local data. The public repository must exclude video, model weights, databases, secrets and large artifacts. Use the staged publication guard and inspect staged changes before each push.

Run `uv run hogline serve` for http://127.0.0.1:8765/ with one bounded worker. A server was running at this checkpoint; recheck instead of assuming it survives chat/tool interruptions. The browser has a Players gallery and an **Only uncertain matches** filter. Automatic-analysis preview now exposes its finite delivery-refinement allowance under the shared time/data budget.

The ignored `data/` directory is roughly 550–600 MiB and is **not backed up by Git pushes**. It contains the catalog, evidence, dense Parquet observations, local models and evaluation/validation exports. Preserve the catalog, evidence and model weights together. Local source originals registered by reference need their own backup. A source-only checkout does not include this archive.

## Implemented since the previous handoff

- [Sampled house states](docs/HOUSE_STATES.md) and [frozen interval benchmarks](docs/HOUSE_EVALUATION.md) derive stability and uncertain configuration-change brackets, preserving original PTS and visibility abstentions. Zero detected handles do not establish an empty house. Interval labels based only on retained context frames are excluded from interval benchmarks.
- [Automatic person processing](docs/ACTORS.md) adds bounded perspective-camera YOLOX person proposals, torso/position continuity and anonymous body fragments to detailed windows. Optional [pose/handle evidence and release hypotheses](docs/DELIVERIES.md) preserve limb joints, colored-object proposals and conservative control/separation evidence. Pose/model failures abstain without suppressing overhead analysis.
- [Anonymous player groups](docs/PLAYERS.md) have stable IDs, optional names, exact-frame body thumbnails, revisioned fragment moves, split/correction support and release exports. Automatic links require at least three identical original frame/body observations over one second, in the same source/view/profile. Competing groups remain separate. Legacy tracks lacking frame hashes do not receive guessed cross-window identity. These groups include adjacent-sheet people/spectators and duplicate fragments; they are not verified participant counts.
- [Local training](docs/TRAINING.md) uses frozen complete reviewed handle views to train bounded CPU random forests. Weights, features, thresholds, training cohorts and model versions are checksummed. Validation/test rejects recording/day/group overlap, including historical source metadata aliases. Registered models can be benchmarked from cached evidence; training does not change the default HSV detector or grant automatic acceptance.
- Source-grid decoding prevents cadence drift and lets nested-cadence overlapping windows retain identical original frames. Short windows support up to 30 native sample targets per second within the 240-frame cap. The source origin affects selection only; emitted PTS remain original. Active configuration start frames are retained directly per view, avoiding the former eight-frame buffer assumption.
- [Automatic campaigns](docs/AUTOMATIC.md) now insert a finite delivery-refinement stage when supported release/control brackets exist. Default allowance: two four-second native-cadence windows; at most eight. No-control motion does not trigger acquisition. All stages share the existing cumulative budget; refinements do not recurse. Source, actor, delivery, decoder and selection versions are pinned. Board-change refinement keeps its separate bounded workflow.

Earlier metadata-first ingestion, inventories/discovery, clock/board reading, game/end review, split/merge reconciliation, moment search, score-disagreement review and immutable releases remain available. See their README links for contracts and measured limits.

Version 0.30.1 fixes a Linux CI timing failure: final publication budget/lease checks use the owning transaction instead of opening a second SQLite writer. Publication reserves a consistent write snapshot, rechecks both job and shared limits, and includes publication time in the reported window cost. A forced slow-publication regression test covers the heartbeat boundary.

## Local validation and useful pointers

The archive has 12 recordings across all eight sheets, about 47 hours inventoried, 61 detailed windows and 13 unconfirmed game intervals. At the last check: six actor analyses, four release analyses, 148 indexed body fragments in 133 groups, nine uncertain group matches and no assigned names. These are historical local counts, not distinct people, accepted shots, completed games or production accuracy.

- Real Sheet 3 source `46723bc6-23ab-4e93-8006-a157b070d2ca`: 5380–5410-second window `58bb31f5-e158-59a0-b87a-beeb70be38e2` retained 787 pose proposals and ten moving handle fragments, with no supported release. The interval is a useful retained model failure case.
- Source-grid window `6f502938-642e-518b-be8f-ead58f7dbc7a`, 5400–5412 seconds, used 9,527,146 evidence bytes and 19.60 worker seconds. Its 21 new fragments made twelve exact-overlap links; nine ambiguous overlaps stayed separate. See ignored `data/source-grid-real-validation.json`.
- Native refinement window `af22eece-e944-54b9-a87c-d712d8aec6ea`, 5392.667–5396.667 seconds, kept 120 samples per view, 8,989,084 evidence bytes and 32.42 worker seconds. It still abstained on release. Report: `data/real-native-delivery-refinement-validation.json`. Initial obsolete job `b06b3f6d-64c2-4431-bbea-b9e0118d8458` failed before publication because of the eight-frame anchor bug. Fresh method-pinned job `9e477e3c-3eb0-4ae5-ba6b-eb3d607b500d` completed after the fix; preserve that failure history instead of retrying the obsolete payload.
- Experimental classifier `opencv-handle-forest-v1:1e91b0d7bc4ea07f` trained on dataset `53299ad3-ce42-4bbd-8ab5-9b978fa216d6`: 19 development views, 45 positive handles, eight negatives, January 16/March 17. Training-day evaluation matched all 45 with no extras. This is training fit.
- Independent assistant-visual March 7 Sheet 4 dataset `9e077f3a-9a98-4760-bbbe-24305a2e5c5d` froze six views from 600/1800/3000 seconds. Default HSV matched both visible handles; the learned model matched one and missed one. All six views were evaluated, with no extras. This tiny test catches a recall regression and keeps the model inactive. Keep these frozen examples out of training when describing them as held out. Public source ID: `ae801f6c-81d7-4be8-a371-ae12bae35c98`.
- Local exploratory release `6e9a1de7-99f2-46b0-b42d-d959ded1bcb5`, `data/checkpoint-0.30.0-exploratory.json`, SHA `fe8bcad85cbad18a1f5ea3e5b8120f078d456db2b1c3c34c55e338262f511e84`, passed current reference validation. It is a local snapshot, not public deployment or accepted statistics.

Pinned YOLOX and MediaPipe pose weights are installed only under ignored `data/models/`; exact hashes, official sources and licenses are documented in ACTORS/DELIVERIES. The learned classifier is under `data/models/trained-handles/`. Public commits contain code/documentation/tests, not footage or models. Synthetic tests establish contracts and abstention behavior; they do not prove real game accuracy.


A fresh version 0.30.1 campaign on March 7 Sheet 4, source 3000–3030 seconds, completed all eight admitted units: one sparse scan, two ordinary detail windows, one automatically selected native-cadence refinement, and four retained-frame OCR jobs. It spent 113.70 worker seconds and reserved 17,914,825 evidence bytes (17.1 MiB) under a shared 180-second / 64-MiB allowance. Release extraction still abstained; completion validates bounded orchestration, not score or thrower accuracy. Local batch `d7cb2dbc-b7ce-4c92-bcb6-d1fcca863583` and ignored report `data/automatic-0.30.1-real-validation.json` retain the exact selections, inputs and outcomes.

## Next work

Improve the corrected stone/object and delivery corpus across independent sheets/days, including brooms, clothing, occlusions, near/far delivery and both ends. A trained instance/footprint model is likely more useful than further color-threshold tuning, but requires appropriate granite labels and representative independent evaluation. The current classifier only filters HSV proposals and cannot recover their misses.

Validate real releases and timing endpoints, robust stone footprints and camera geometry, full multi-game-day segmentation and reconciliation, and stronger scoreboard coverage before enabling automatic score/shot acceptance or per-player weight statistics. Named rosters are no longer a prerequisite for the player workflow. Product automation should keep improving without silently treating unknowns as zeros or increasing acceptance based on training fit.

Do not turn missing observations into zero scores, confuse board-posting time with end completion, count per-view hypotheses as physical shots, or present synthetic/selectively sampled results as archive-wide accuracy. Preserve source timing, correction history and bounded on-demand acquisition throughout.
