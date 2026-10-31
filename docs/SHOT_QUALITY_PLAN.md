# Shot calls and player consistency: proposed next steps

The goal is a reviewable report per person or anonymous game slot, not a score inferred from the final stone position alone. Positions can rotate by end; a skip's tactical role and last thrower are separate. Start with editable red/yellow side and lead/second/third/fourth slots per end, then link them to confirmed people when available. Preserve substitutions and unknowns.

For each delivery, collect independently reviewable evidence with timestamps and source views:

1. Delivery order, stone color and thrower association; retain alternatives when occluded. Infer a role from repeated throwing order only as a proposal.
2. Pre-delivery broom head, line from hack to broom, turn, pointing and house context. Propose call types and target regions, including hit weight versus draw; preserve an unknown option. A broom marks a line, not necessarily the desired resting position. Strategy can rank possible calls, but is not evidence that a particular call was made.
3. Stone trajectory and uncertainty in calibrated sheet coordinates, plus visible sweeping bouts and their timing. Report missing or occluded segments. Sweeping intensity, brush force and the counterfactual unswept finish are not directly observed.
4. Link a reviewed call and delivery to a player or anonymous slot. Keep human corrections, provenance, model versions, and exclusions alongside each row.

A vision model could propose broom/gesture and scene labels from selected frames, with links back to the original video and operator review. Evaluate it against a held-out, manually labeled set before enabling automatic acceptance; small distant objects and strategic intent may be difficult. Any remote API must be explicitly configured and budgeted, with sample selection, cost limits, and clear disclosure that frames leave the machine. Avoid sending face crops for identity matching; local face suggestions already support review.

## Proposed measurements

- **Line:** signed delivery-line error relative to the called broom, using a consistent sheet coordinate for left/right and a separate turn-aware mapping for inside/narrow and outside/wide. Define the reference moment and viewing direction explicitly; report both signed bias and spread. Exclude shots without a defensible broom, turn, calibration and delivery trajectory.
- **Weight:** compare a measured release speed or calibrated split time with a target distribution for the reviewed call, shot type and conditions. A ratio of measured to target speed can give an intuitive multiplier (above 1 faster, below 1 slower), but must be calibrated against intended outcomes and is not a literal measure of how much sweeping was needed. For hits, use an appropriate hit-weight target; do not mix draws and hits. Report mean or median ratio and dispersion in the same units, number of shots, coverage and uncertainty. A hypothetical 0.9 ± 0.0 indicates consistently slower than the chosen target, while 1.0 ± 0.5 indicates an average near target with large variation; neither is a validated rating scale yet.
- **Team-assisted result:** show final error from the reviewed target separately, together with observed sweeping. Do not credit all of that result to the thrower or assume more sweeping means a worse release. Sweeping is a response to both line and weight, and its effect depends on ice and technique.

First build a small review set across sheets, directions, shot types and lighting. Measure call/role agreement and target, trajectory and line errors against independent labels, including unknown cases. Establish minimum sample sizes and uncertainty before player comparisons; show per-shot evidence and excluded counts. Calibrate the weight multiplier with controlled shots or validated physical estimates before treating it as a coaching score. Until these inputs exist, show coverage and missing evidence rather than fabricated ratings.

## Hierarchical action segmentation and opposite-end feeds

Prioritize a game → end → delivery → moving-stone hierarchy alongside skill reports.
The user confirmed the Wausau layout convention: on **odd ends**, the right
perspective feed shows the thrower delivering toward that camera, while the left
feed shows the skip at its far receiving house. Even ends reverse. The rule is
stable across weeks and leagues for this layout, rather than being inferred from
clothing or roster identity. Face work is being handled separately; leave it out
of this task.

Use opposite-end sightings to follow one physical stone, not to count two throws.
Origin proposals require controlled-delivery evidence at the far house; visibility
of a traveling stone in a camera is insufficient. Camera/frame latency still needs
checking before merging precise motion measurements. Shared source PTS do not by
themselves prove the underlying feeds have zero latency.

Direction changes support end-boundary brackets and odd/even parity. They cannot
by themselves distinguish ends 1/3/5 or prove that no whole end or throw is missing.
Use reviewed game-start/end anchors, end windows, scoreboard evidence and missing-
throw-aware order constraints for exact labels such as “Red lead, stone 1, end 1”.
Keep fourth throwing position and skip duty separate.

For each delivery retain independently supported start, release, delivered-stone
stop/out-of-play, and all-action-settled timestamps. A takeout can remove the thrown
stone while other stones continue moving, so an action-only reel should normally
end after the relevant collision motion settles. Disappearance or the end of a
short delivery clip is not a stop observation. Preserve censored/unknown endpoints.
Once completion evidence exists, export source/time edit intervals with suitable
padding and merge overlaps to avoid duplicated action. The current hierarchy JSON
has explicit null finish times; it is not yet a usable complete action-only EDL.
