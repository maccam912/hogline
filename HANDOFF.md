# Hogline handoff

Checkpoint: October 28, 2026. The implementation is a working local pilot, not the completed system in [DESIGN.md](DESIGN.md). The previous work stopped because the requested weekly-usage reset was observed, not because the design was finished.

## Starting a new session

Work in this repository, read [README.md](README.md) and the relevant parts of [DESIGN.md](DESIGN.md), then check `git status`, the current branch and remote before making changes. The user wants progress committed and pushed as it is made. The public repository must not contain secrets, video, model weights, databases, or other large artifacts; use the existing staged-file guard and review the staged changes before pushing.

The latest implementation checkpoint at this handoff is version **0.23.0**, commit `67554b3`. The local server was running at http://127.0.0.1:8765/ with its worker enabled. Start it with `uv run hogline serve` if needed; setup and dependencies are in the README. The CI run for that checkpoint succeeded, and 228 local tests passed. Recheck these facts rather than assuming they remain current.

The local `data/` directory is ignored by Git and was approximately 494 MB at this checkpoint. It contains the catalog, evidence, local exports, evaluation data, and test artifacts; it is **not backed up by pushing this repository**. Preserve it, and back up the catalog and evidence together if relocating the project. A fresh clone will have code and documentation but not this archive. Local originals registered by reference also need their own backup.

## What is available

- Recordings can be discovered and inventoried from metadata without downloading entire streams. Bounded jobs fetch individual frames or short windows as needed; limits and actual caveats are in [processing](docs/PROCESSING.md) and [automatic campaigns](docs/AUTOMATIC.md).
- Sparse activity candidates, scoreboard/clock observations, game and end review, split/merge reconciliation, timestamped YouTube links, immutable releases, and cached CV evaluations are implemented. [Moment search](docs/MOMENTS.md) links evidence across recordings; [score disagreement review](docs/SCORE_DISAGREEMENTS.md) flags conflicts without silently changing saved scores.
- OpenCV proposes colored handle locations and conservative track fragments. A real Sheet 5 movement example has timestamped evidence, but fragments are not verified physical stone or shot identities; see [tracklets and validation](docs/TRACKLETS.md). Board reader evaluation, source checks, and known limitations are in [board evaluation](docs/BOARD_EVALUATION.md) and [cross-sheet checks](docs/CROSS_SHEET.md).
- The last live checkpoint contained 12 recordings from all eight sheets, about 47 hours of inventoried footage, and 13 unconfirmed game intervals. Four flagged metadata refreshes completed without changing their manifests; none remained pending then. These are historical counts, not claims about the current database or completed games.

## Next work

Prioritize stronger automatic detection of stable house states and end transitions, supported by representative independent labels and cross-sheet/day evaluation. Validate a full multi-game day and a real confirmed overlapping or continuation pair. Improve scoreboard coverage and obstruction handling before considering automatic score acceptance. Calibrated stone footprints, physical tracking, identities, historical rosters, production operations, and season-scale validation remain open. The README and design document contain the fuller list and intended data contracts.

Do not turn missing observations into zero scores, confuse board-posting times with end-completion times, treat proposed fragments as shots, or present selectively sampled/synthetic results as archive-wide accuracy. Preserve exact source timing and links so a person can verify conclusions. Respect bounded on-demand acquisition; the user does not want the entire stream archive downloaded up front.
