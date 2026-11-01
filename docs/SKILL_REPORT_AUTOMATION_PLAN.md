> October 31 follow-up: the native delivery-house acquisition, approximate straight-ray transfer, source-image overlay and timing-estimate slice is now implemented and populated for all 48 selected deliveries. This does not complete automatic shot judging or weight forecasting. See [native delivery rays](DELIVERY_RAYS.md) for the measured scope, API, provenance, limits and next stopping-distance milestone.

# Automating useful player reports

October 31, 2026. This is a design and implementation plan, not a claim that automatic shot judging already works.

## Desired result

After a recording finishes, Hogline should publish a useful report for each identified player without requiring an operator to locate every throw, name every actor fragment, calibrate every camera, or classify every call. Publish supported observations and explicitly labeled estimates first. Add measured statistics when their prerequisites are available. An unknown target must not suppress unrelated observations about a visible delivery or outcome.

The normal operator input should be the team/stone-color assignment and existing named or anonymous player links. Names remain optional. Missing color or ambiguous identity should produce separate anonymous observations and a small unresolved question, not force a guessed team or person. Optional call notes and corrections improve a report but are not prerequisites for a report to exist.

## What this game taught us

The October 30 Sheet 4 Koski–Dobbs review is the development example. The user explicitly established that Koski played yellow and described repeatedly moving the broom after giving a shot to indicate where the player actually aimed. The named lineup already existed. Individual shot attribution, end labels and outcomes below are assistant interpretations, not human-confirmed ground truth.

| Finding from the review | Consequence for automation |
| --- | --- |
| The named lineup and anonymous skill actors were disconnected. | Join delivery evidence to the existing recording-specific player identities; do not ask the user to name everyone again. |
| The run had 248 completed vision clips, 75 consolidated delivery candidates and 75 local motion clips when inspected. It had exhausted practical reservation capacity under the $10 cap before completing the survey. | Avoid sending successive generic 30-second windows to an LLM across the whole recording. Use local event discovery, then spend on bounded, relevant evidence packs. These counts are this run, not cost or accuracy guarantees. |
| Assistant review attributed 48 apparent Koski deliveries across six ends, 12 per player. Ten deliveries were absent from the automatic list: eight Angela and two Ethan. | Find scan gaps independently. Expected order is a check and search prior, never permission to fabricate missing throws or force a count of 48. |
| Seven Angela deliveries appeared to finish in front of the rings. The overhead house view alone could not show their results. | A finish search must include the receiving perspective view and guard area. “No new stone in the overhead house” is not a failed shot, missing stone, or zero accuracy. |
| Lindsey's sixth-end pair includes an inner-house stone followed by a front-of-house placement. | Separate observed final position from intended call. A short-of-rings placement can be a guard rather than a short draw. |
| Several Matt shots visibly move/remove reds, but some final shots are followed immediately by cleanup. | Separate contact, stable outcome and manual clearing. Do not attribute cleanup movements to the delivered stone. |
| Broom movements could be found, but several were ordinary repositioning or lifting rather than securely identified actual-aim feedback. | Preserve the original call. Treat a later broom indication as a separately interpreted annotation, with evidence and alternatives. Do not replace the call or derive measured aim from every movement. |
| Useful reports were possible without inventing speed, release-angle accuracy, or sweeping effectiveness. | Support observation reports, conditional judgments and metric measurements as separate products. |

The current reports retain 222 selected original frames with PTS and checksums, 48 attributed deliveries and 16 player findings. Their first version was revised after the wider-camera pass revealed front-of-house placements. Preserve that correction history. This is a useful development corpus, not independent proof of model accuracy.

Private artifacts are under `data/skill-reports/`: review selection, native-frame receipts, immutable review requests, report JSON/HTML/Markdown, before/after database receipts and diagnostic contact sheets. They are not in a fresh checkout and should not be committed as biometric/media training data.

## What exists now

- `hogline/domain/skill_estimates.py` consumes the recording's `operator_skill_context` and explicit lineup links, validates assistant-reviewed attributions, supports independent deliveries found in scan gaps, and constructs per-player estimated reports.
- `SkillEstimateRevision` stores immutable, checksummed review revisions separately from confirmed shot measurements. Source/context/roster/candidate/frame changes withhold stale findings. Duplicate intervals and opposing-color attributions are rejected.
- `hogline/orchestration/skill_estimate_frames.py` acquires a bounded number of original frames from verified local footage without paid requests or decoding inside the database writer transaction.
- `GET/PUT /api/games/{id}/skill-estimates`, `/history`, `/report` and `POST /frames` expose review, provenance and printable reports. Player skills links to the reports.
- `hogline/analytics/estimated_reports.py` renders evidence-linked player reports. Estimate evidence is protected by retention.
- The existing strict `shot-execution-v5` report still requires reviewed calls, direction and supported measurements. Assistant estimates do not silently enter it.

The new path still depends on assistant/operator review of each attributed delivery and written findings. It is not yet an automatic judge. The rest of this document describes the next work; do not treat the endpoints above as completion of that work.

## Proposed pipeline

### 1. Establish recording and team context

Keep a revisioned game-context record for stone colors, format, lineup links and optional user conventions. Migrate the current source annotation into that record without changing the source acquisition manifest. Scope the October 30 color decision to this recording; it says nothing about the older Sunday games.

Project this context into report attribution without invalidating valid raw video observations merely because an unidentified team acquired a name. Semantic changes such as swapping colors or correcting identities must invalidate affected derived judgments. Preserve the previous report revision.

Use the existing end/direction hierarchy, native scoreboard evidence and observed changes of delivery end to propose end blocks. Maintain boundary brackets and disagreements. Posting a score does not establish the exact instant an end finished. Do not infer an end number solely from a cropped-camera name.

### 2. Discover all deliveries locally

Use a bounded local pass over the playable interval, with cached person/activity/stone motion and layout masks. Propose delivery starts from setup followed by controlled forward stone movement. Include standing and stick deliveries. Use higher cadence only around likely events.

Look for a person changing from waiting/setup to delivery, the delivered stone leaving the origin house, and subsequent sweeping/receiving activity. A crouch is neither necessary nor sufficient. Deduplicate overlapping windows and multiple views by physical source interval and compatible motion/color evidence.

Compare the resulting sequence with plausible alternating-team order and two throws per position per end. Search suspicious same-color sequences, long activity gaps and missing-role intervals. In this game those checks should direct a local search toward the eight missed Angela and two missed Ethan deliveries. Report unresolved gaps rather than inserting expected shots.

Keep independent, source-timed recovery evidence distinct from original model proposals, as the current manual recovery path does.

### 3. Associate the thrower with an existing player

Connect the delivery actor to recording-specific lineup observations through same-frame body overlap and continuous movement. Match the standing setup frames before the player ducks; the face can be invisible during the slide. Then follow the same body into and out of the delivery.

Reuse the operator's link from anonymous lineup player to person. Clothing, throwing order and teammates can support a proposed association; clothing or position alone must not create a named identity. Check competitors, simultaneous observations and adjacent sheets. Require a margin over competing matches and retain the alternative when it is close.

The report must disclose attribution basis: direct same-frame link, supported continuity, order-supported hypothesis, or unresolved. Weak identity evidence stays anonymous and is excluded from named-player trends. Correcting a player link should regenerate affected attribution/report stages without buying all the original vision calls again.

### 4. Build a complete evidence pack for each shot

Retrieve these phases separately:

1. Receiving-house setup and the original called broom before the delivery.
2. Thrower setup, controlled motion and release bracket when visible.
3. Early stone travel, including whether the player still controls it and whether sweeping/contact has begun.
4. Receiving perspective footage covering guards, rings, back line and relevant side exits, plus the overhead house view.
5. The earliest stable post-shot configuration before another stone arrives or a person moves stones.
6. Any later deliberate broom indication before the stone arrives, with its original call retained beside it.

Use actual source timestamps, camera crop bounds and original frame hashes. Different views of one instant are evidence for one shot. A short tracking fragment ending does not establish that the stone stopped.

Start with a small contact sheet and local trajectories. If the pack cannot distinguish guard versus unseen finish, acquire the wider view or a denser short interval before asking the model to judge. Never interpret an empty overhead result as a miss.

### 5. Ask an LLM to interpret bounded shot evidence

An LLM can be useful for associating visible phases, recognizing contacts/rolls and expressing a conditional judgment. It should not be allowed to fill missing intent from the outcome or publish unsupported precision.

Use a versioned structured response with separate fields:

| Field | Required behavior |
| --- | --- |
| Shot/actor association | Cite supplied source frame IDs and actor references; preserve competing associations. |
| Observed original broom | A visible point, frame, ice-contact status, holder and purpose uncertainty. A parked broom is not automatically a call. |
| Call hypothesis | Known from explicit evidence, likely interpretation, or unknown; list evidence and alternatives. Never derive intent from the eventual finish alone. |
| Delivered stone | Color, visible trajectory intervals, contacts, occlusions and any association uncertainty. |
| Stable outcome | In rings, in front of rings, past house, side/out, contact outcome, or unknown. Show the relevant wider-camera and overhead frames. |
| Execution judgment | Evaluate against a known call, or give clearly separated conditional interpretations. With unknown intent, describe the outcome without a made/missed grade. |
| Aim feedback gesture | Original point and later point, holder continuity, gesture timing and interpretation alternatives; keep it separate from the called line. |
| Limitations | Missing views, unresolved release/finish, sweeping/contact confounds, cleanup overlap, weak identity or unreadable target. |
| Evidence citations | Every factual conclusion cites frames actually supplied in this request; reject nonexistent and foreign-source IDs. |

Give the model strict instructions to abstain on hidden points and separate observation from tactical inference. Ask for qualitative confidence tied to evidence; do not display model-generated probabilities as calibrated accuracy.

Validate schema, frame/time scope, phase ordering and identity/color consistency locally. Retry only a narrowly repairable response within the frozen plan's allowance; do not pay repeatedly to force a confident answer. A second independent judgment can be reserved for contradictions or unusually consequential findings, not requested for every easy shot.

### 6. Interpret actual-aim broom indications

The user's convention supplies useful meaning but does not make every movement an example of it. Require evidence of the same skip holding the original call, a shot underway, a deliberate later indicated point, and no simpler explanation such as lifting the broom to clear play, preparing the next call, cleaning or repositioning a stopped stone.

Save two separate events: `original_called_line` and `skip_indicated_actual_aim`. Keep the latter labeled as the skip's assessment, with interpretation status and supporting frames. It is not a measured initial stone path.

Prefer lateral comparison on the same visible tee line. With a checked nominal house scale, provide an approximate displacement and image-direction convention, retaining point/geometry uncertainty. Do not use an elevated broom tip as an ice coordinate. Wide/narrow also needs curl direction and delivery orientation. If the gesture is ambiguous, show a candidate clip without adding it to an aim-error average.

Compare an interpreted gesture with a supported early post-release track when available. A disagreement is evidence to review, not permission to overwrite either observation.

### 7. Aggregate into useful player reports

Publish one card per named or persistent anonymous player. Include coverage, attribution quality, a few representative positive outcomes, repeat patterns, conditional review priorities, counterexamples and direct clip/frame links. Unknowns remain visible and do not count as zeros.

Distinguish:

- **Observed/estimated outcomes:** placement region, visible contact/removal, guard position, supported finish depth and individual examples.
- **Intent-based execution estimates:** qualitative or coarse grades only when the intended call/target is supported. Separate guard, draw and hit cohorts. Show the number actually judged and the number withheld.
- **Measured statistics:** calibrated speed, target-speed ratio, release direction and other metrics only when their existing physical/evidence checks pass. Nominal geometry remains labeled approximate.

A stone in the house is not necessarily a made shot, a guard is not necessarily a missed draw, and a removal with a rolled-out shooter can be correct for one call and wrong for another. Do not rank players by raw house-counter counts. Do not infer sweeping effectiveness from seconds swept or the final stone position alone.

Let report generation use sparse but useful evidence. Fewer than five comparable observations should remain an individual-example report, without a stable-bias claim. One changed shot or corrected identity should revise the affected player findings and preserve the prior version.

### 8. Run automatically within bounded work and spend

Integrate finite stages into `recording_followups.py` and the existing durable worker/campaign machinery: local discovery, identity association, evidence packs, selective LLM judgments, reconciliation and report publication. Persist plan hashes, per-unit jobs, elapsed time, image growth and coverage gaps.

Keep the approved vision cap, reservation accounting and single-vision-job admission. Cache model results by evidence, prompt/schema/model version and task purpose. Reuse decoded frames and judgments across report revisions. Do not rerun a generic whole-recording LLM survey because one named-player link changed.

Plan the remaining cost before dispatching. Prioritize missing-player coverage and representative calls, then difficult gaps. Reserve some capacity for resolving contradictions rather than spending everything on the first survey. The current approximately $9.90 ledger commitment is conservative accounting with retained reservations, not a verified bill. This plan authorizes no increase and starts no new paid work.

Failed/uncertain units become report coverage gaps. Continue independent local work; preserve operator pauses. A budget pause should still leave the current estimated report readable. Show “report available with gaps,” “judging shots,” and “awaiting review” distinctly from “confirmed measurements.”

Retain one original parent frame plus reproducible crop descriptors, and protect cited review evidence. Keep the existing 2 GiB cache bound; do not solve report retention by raising it. Reacquire bounded source intervals when a missing asset is needed and the source is still available.

## Delivery sequence and acceptance

| Milestone | Deliverable | Acceptance evidence |
| --- | --- | --- |
| A — delivered in this review | Estimated report/revision API, named-lineup context, original-frame capture, printable reports and retained evidence | Four live Koski reports; 48 assistant-attributed deliveries, 16 findings, original-frame links and immutable revisions. Existing strict reports remain separate. |
| B — local coverage and identities | Automatic delivery discovery/recovery and named-player association | Replay this recording without its manual assignments supplied to the algorithm. Recover missed examples or explicitly report gaps; no invented throws or competing-sheet assignments. Compare candidate recall, duplicate rate, and identity errors separately. |
| C — outcome evidence | Wider receiving-view tracking and stable-outcome/cleanup classification | Front-of-rings placements remain visible, including the seven Angela examples. Cleanup fixtures do not create credited contacts or final positions. End direction works both ways. |
| D — bounded LLM judge | Versioned evidence pack, structured interpretations, validation and selective repair/escalation | Every judgment resolves to supplied original frames. Unknown intent cannot produce an unconditional success grade. Ledger/caching/restart tests prove no duplicate charging. |
| E — report automation | Follow-up orchestration and automatically revised per-player findings | An analyzed recording produces readable reports without per-shot operator entry, within its frozen budget. Pauses/gaps remain visible; a correction revises affected claims and keeps history. |
| F — independent evaluation | Held-out recordings and independent labels | Validate delivery recall, identity attribution, outcome classes, call judgments, abstention and cost on other games/days. Do not treat agreement with this assistant's own labels as independent accuracy. |

Start B and C before expanding the model budget. They address the clearest failures of this run and improve the evidence available to any judge.

## Required regression and review cases

- Guards outside the overhead crop, deep stones, side exits, rocks that roll out after contact, occlusion and both delivery directions.
- Standing/stick delivery, setup mistaken for a throw, a thrower with no visible face during the slide, clothing similarities and neighboring-sheet activity.
- Overlapping candidate windows, duplicate views, missing throws, unknown stone color, color contradictions and corrected lineup links.
- Original call versus later feedback, a lifted tip, a parked broom, ordinary sidesteps, sweeping/cleaning, and preparing the next call.
- Immediate post-shot cleanup; multiple stones moving because a person cleared them; genuine contact before cleanup.
- Different calls with similar finishes; unknown target with a visible outcome; fewer than five comparable shots; no inferred unswept weight or sweeping force.
- Source/parent/frame corruption, stale report suppression, retained evidence, reproducible versions, concurrent correction and bounded data growth.
- Model timeout, malformed/unsupported claims, missing evidence citations, budget pauses, restart/resume, idempotent requests and zero-cost local work while vision is paused.
- Live report/UI checks: all players present, per-player clips work, unknown counts are explicit, no hidden model-confidence-as-accuracy, and readable saved exports.

Measure the cost and review burden per game alongside correctness. The user should receive a useful report without being asked to reconstruct the video manually.
