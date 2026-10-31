# Player skill review and vision pilot

The **Player skills** link opens `/static/skills.html`. It supports proposed game spans, anonymous participant labels assigned to each end/side/throwing position, separate skip assignments, editable shot observations, and descriptive weight/aim reports. Naming a participant is optional. Confirmed assignments establish continuity across ends only when the same participant label is deliberately reused.

Throwing-order proposals assume standard four-player order (deliveries 1–2 lead, 3–4 second, 5–6 third, 7–8 fourth). They retain competing candidates and never infer skip from fourth. Reviewed end intervals and observed deliveries produce possible throwing-order assignments while preserving missing-shot ambiguity. Game-local continuity proposals connect shared delivery observations or identical frame crops; accepting a participant link remains explicit. Do not apply this assumption to doubles or other formats.

Reports include only confirmed shots with known call and direction. Weight is measured speed / target speed at a shared calibrated reference plane. Line is degrees right-positive from the called broom, looking toward the target house. Wide is outside-positive relative to curl: right of broom for left curl, left of broom for right curl. Both weight and aim require a calibration identifier. Call, direction, turn and calibration define separate cohorts. Unassigned anonymous slots are scoped to the end to avoid silently merging different people. Conflicts with confirmed slot assignments are excluded.

Mean ± sample standard deviation is descriptive; it is not a confidence interval. Fewer than five observations is flagged insufficient; one observation has no spread estimate. Sweeping duration is descriptive and does not adjust release weight or infer an unswept finish. Actual reports still need reviewed targets, calibrated trajectories, and valid thrower links. No validated player accuracy ratings exist yet.

`GET/PUT /api/games/{game_id}/skills` reads and revisions a complete review document. Updates check the displayed revision and shots must lie inside a game source span. `GET /api/games/{game_id}/skills/history` retains changes. Splits/merges require review of the replacement game. Free-form operator references are retained. Structured sequence frame references are checked against the owning game, source metadata, frame index and checksummed sequence result.

## Local motion batches

Under **Inspect stone motion**, preview a batch of 1–12 existing delivery candidates,
then start it. The preview lists the chosen clips and reasons for omissions. Current
motion results are reused, including current refinements; excluded, ambiguous or
unsupported candidates are skipped. Unknown release timing is labeled and uses the
end of the observed delivery action to select the clip. Clips stay within game spans.

The batch admits one local motion job at a time across all games, survives restarts,
and supports revision-checked pause/resume/cancel. Pausing stops future admission;
an admitted clip may finish. Changed reviews, source/anchor evidence or processing
methods pause the batch. Failed clips are surfaced rather than automatically retried.
No OpenAI configuration or additional API calls are needed for this stage. Completing
tracking does not confirm stone identity, free travel, player roles, calls or scores.

## OpenAI pilot

`.env` is ignored by Git; `.env.example` is the template. Settings are loaded literally without shell execution, and environment variables override the file. Configure `OPENAI_API_KEY`, `HOGLINE_VISION_MODEL=gpt-6.1-terra`, `HOGLINE_VISION_ENABLED=true`, and `HOGLINE_VISION_BUDGET_USD=5.00`. The key is never returned by status endpoints or saved in job results. The pilot sends selected frames to OpenAI's Responses API with `store=false`; local face matching remains separate.

The initial runner is `hogline.perception.vision_calls.propose_call(data_dir, frames)`, where frames are up to four `(timestamp label, PIL image)` pairs. It scales images to at most 1024 pixels per side, caps output at 4000 tokens, uses Terra on standard service, and validates structured output. Results stay proposed. No automatic retry occurs on uncertain failures. A SQLite ledger reserves $1 before each request, including failed/uncertain requests, against the configured budget. Reservations are deliberately conservative and are not actual billed spend. Current successful calls release most of the reservation using a conservative token-cost estimate; older requests without complete usage accounting keep their reservations. Usage is saved with each response. Do not increase the budget just to recycle reservations without reviewing that ledger.

Two initial calls on Jan 26 Sheet 8 succeeded: a retained preview and a four-frame 30-second sequence. Both found visible broom/sweeping evidence and abstained on intended call. The sparse sequence missed the delivery slide, demonstrating why denser delivery-centered clips are needed. Original pilot frames and PTS are under `data/skill-vision/pilot-jan26/`; response documents are under `data/skill-vision/`. The review page displays these proposals. The durable sequence worker and draft-review transfer described below supersede this initial runner.

The selected recordings were found from current `manual_preview_label` decisions: Jan 26 Sheet 8, Feb 2 Sheet 3, Feb 23 Sheet 2, all labeled Koski Team. Those labels apply only to preview moments; game boundaries and sides still need evidence. Existing dense windows on these recordings lack usable release features and need reanalysis for automatic delivery association.

Official references: [image inputs](https://developers.openai.com/api/docs/guides/images-vision), [Terra model](https://developers.openai.com/api/docs/models/gpt-6.1-terra), [pricing](https://developers.openai.com/api/docs/pricing).

Development checkpoint: a proposed partial game excerpt (source seconds 1800–1920) was created for Jan 26 Sheet 8, ID `9e56460a-9aa1-5d98-b53d-d9e9c48a391d`. It deliberately has no accepted positions, sides, ends or shots. Browser inspection verified the live review screen on the separate no-worker development server at port 8767. The existing processing server on 8765 was left running. The full suite passed 491 tests before the latest endpoint test; the focused skill suite then passed eight tests including HTTP revision conflicts. Next work: densify delivery capture, add durable proposal jobs and evidence acceptance, connect calibrated measurements, extend anonymous role continuity and validate reports on real reviewed shots.

## October 30: durable sequence processing

The main server at port 8765 now includes **Detect deliveries and calls**. It queues bounded sequences (2–16 samples, at most 30 seconds), with an optional end-camera or house crop. Worker leases, source/game revisions, input checksums, original presentation timestamps, retained contact sheets and API response provenance are recorded. The result stays proposed. **Review shot** transfers a detected delivery and its frame references into a draft; unknown end, side, person and measurements remain unfilled. Confirming requires a known end/side and evidence. Sequence evidence is validated against the game and current source metadata.

Two real durable jobs succeeded for Jan 26 Sheet 8: a 12-frame composite sequence at 1849–1861 seconds and a 16-frame left-camera sequence at 1857–1865 seconds. Both proposed the same red-stone delivery and abstained on exact release and call. The cropped proposal was saved through the live UI as an unconfirmed shot. No weight or aim rating was produced. These are two observations of one delivery, not two independent deliveries. Cross-sequence candidate consolidation now groups their overlapping same-color observations into one delivery candidate.

Vision requests now have a payload/context key. Completed requests reuse a checksummed result; an unresolved earlier attempt blocks automatic recharging. Successful calls release most of the reservation using a conservative token-cost estimate ($3/M input, $9/M output, rounded upward to cents), rather than claiming exact billed spend. Old pilot requests without those ledger fields keep their reservations. Actual usage and response IDs remain in the artifacts. Reservations plus estimates enforce the configured pilot cap. No credentials are stored in results.

Reports now pool opposite turns within matched call, travel direction and calibration, so a consistently wide player can have near-zero left/right bias but positive outside bias. Turn-specific groups remain available in the API. Unknown people with no known throwing position are excluded rather than pooled into one invented player. Duplicate confirmed delivery numbers/source moments are rejected. Saved reports become stale when the source or game changes.

Validation at this checkpoint: 500 tests passed, including 16 focused skill/sequence tests. Live browser checks verified the detected delivery could be transferred and saved as a proposal, with the report explicitly excluding it. Screenshot: `data/skill-vision/review-proof.jpg`. The main server was restarted only when the worker queue had no running jobs, retaining the user's 16-worker/four-recording configuration.


## October 30: delivery consolidation and local motion

Overlapping same-source, same-color delivery observations are consolidated into proposed candidates. Reviewing another camera observation reuses the saved shot instead of silently making a second shot. Conflicting call proposals remain unknown. Multiple confirmed shots linked to one candidate are rejected at save time and excluded from reports if encountered in existing data. This is conservative interval association, not verified physical identity.

**Inspect stone motion** queues a local CV job through `POST /api/games/{game_id}/skill-motion`. It accepts a house view, 3–160 samples across at most 16 seconds, and a clear reference frame at or within 120 seconds before the clip, all inside the game spans. No OpenAI request is used for this stage. Reference and sample PTS, full decoded RGB hashes, crop hashes/bounds, frozen profile/source/game versions, raw candidate rejections, track gaps and measurements are retained in checksummed results. Geometry changes in image dimensions stop the job. Camera movement without a dimension change remains an explicit unverified assumption.

The painted-house fitter now accepts red or green inner rings. Dense tracking uses an opt-in faster association gate while existing sparse tracking retains its defaults. A local colored-patch size prior rejects tiny red/yellow patches, including the observed red-shoe false track in the pilot. This is a Wausau development prior, not proof of a stone or a measured granite diameter. Missing detections split fragments. The measurement fit stays within the supported house region and uses nominal ring dimensions, with pixel sensitivity and fit residual recorded separately from physical uncertainty. It cannot establish unswept release weight, call intent, or aim by itself.

Each result retains the exact native reference crop, a trace overlay and up to 12 original sample crops; these are viewable in the skill page. Worker time, decode counts, 128 MiB in-memory crop storage, 16 MiB retained evidence per job and the shared image cache are bounded. Recording cleanup now recognizes published skill sequences and motion assets as owned evidence, so it cannot delete them as orphan files. Explicit archival/eviction of old skill results is still future work; the shared cache cap can stop new acquisitions.

A real job on Jan 26 Sheet 8 succeeded for source seconds 1857–1861 at 0.1-second cadence, using the house-top reference at 1833. It retained 40 sample measurements and eight review frames. Two moving fragments cover 1857.70–1858.00 and 1858.70–1859.00, with nominal painted-plane speeds about 2.53 and 2.61 m/s. They belong to one observed delivery, not two throws. Inspection shows the thrower still controlling the stone; **neither is a release-weight score**. The red ring obscures the handle between fragments. Call, release, player role and intended target remain unconfirmed.

The first live attempt exposed a one-frame reference budget error. The corrected job implementation is `bounded-house-motion-job-v2`; a real FFmpeg test now covers this path. The failed attempt remains in job history. Successful motion result: `ff6994d4-997d-583c-bce2-7b2e9546e264`, job `b52c3e92-59cb-4b23-8da9-c35b5d7271f2`. Browser evidence: `data/skill-motion/motion-review-proof.jpg`.

Next: establish visible release/broom observations and delivery order over longer game excerpts, evaluate anonymous role proposals, and connect independently reviewed calibration/targets to measurements. Automatic ratings are still withheld where these inputs are missing. Short proposed excerpts from the two other team-visible recordings have now been processed as described below; they are not complete games with validated reports.


### Expanded excerpt pilot

Two more proposed partial game excerpts were created around the user's team-visible preview labels, without assigning team sides, end numbers or identities:

- Feb 2 Sheet 3: game `7da6f04d-4f28-50c8-aeb6-8d642999d8bc`, source span 1957–2077. The 15-frame composite vision job at 2003–2033 succeeded (`950be2b4-318b-590c-b5ff-3d9146b922a6`). It found sweeping evidence but no delivery/release or established call. Original sample grid starts at 2004, as recorded in the PTS provenance. A local motion job at 2012–2020 using the bottom-house reference at 2004 completed with unavailable measurements: the reference fit failed its held-out inner-ring residual criterion. Result `5d4553a4-a916-5262-8d45-b3efedd43da5` retains the reference and diagnostics. No accuracy score was inferred.
- Feb 23 Sheet 2: game `d0828cb6-d6e6-5264-9613-7f2a2f3674ed`, source span 1988–2108. The 15-frame composite job at 2034–2064 succeeded (`deaf89c6-f42d-535d-93b9-d8b66533c703`), retaining unknown call and ambiguous stone-handling actions. No delivery was confirmed.

The next useful fallback is to retain image-space motion evidence even when physical projection requires review, while withholding metric speed. Longer delivery-centered coverage and anonymous activity/role proposals are still needed.

Current validation: 516 tests passed, Ruff and diff whitespace checks passed; the live main server includes the motion worker and published-skill evidence retention. The `.env` remains ignored with mode 0600 and the template contains no key. Changes remain local in the existing shared worktree.


## October 30: anonymous activities and image-motion fallback

Sequence schema `curling-sequence-v3` adds frame-local anonymous actors, body boxes, key frames, activity intervals, proposed team sides with their basis, and links to observed deliveries. Activities distinguish delivery, sweeping, deliberate calling-broom gestures, cleaning, stone handling, walking, waiting and unknown. Thrower links require an overlapping delivery activity; dynamic delivery/sweeping requires multiple supplied frames. Bounds, cross-references and finite normalized boxes are validated. The prompt explicitly separates clothing color from team color and does not identify or name people.

The review page shows anonymous thrower/sweeper/broom-holder proposals and native participant crops. Each actor ID is scoped to its sequence, source and game. Saving a shot rejects absent actors, foreign-game actors and reuse outside the clip; named or cross-clip continuity still requires deliberate operator assignment. Reviewing a detected delivery proposes its linked thrower and reuses an existing shot candidate. It does not infer lead/second/third/fourth or skip from one action. The existing per-end throwing-order proposals can use the actor once a delivery number is established. A participant draft is cleared when changing games.

The live Jan 26 cropped run produced one red thrower, two sweepers with unknown team side, and one waiting participant. Result `30b0117b-b433-5d76-8611-e377af4c76f8` links A1 to the observed delivery. Native crops were visually checked. The existing shot was updated through the live UI as a proposal at skill review revision 2; its end, call, release and throwing position remain unknown. It remains excluded from player statistics. Screenshot: `data/skill-vision/actor-review-proof.jpg`.

A Feb 2 composite run (`d5511015-8453-557a-ae3a-b89d6b5993d6`) proposed two sweepers and a walking participant, retaining unknown team sides and call. No delivery was invented. These clip-local tags do not establish continuity with people in another recording, even if clothing looks similar.

Motion job `bounded-house-motion-job-v3` continues image-space tracking when projection is unavailable/requires review. A crop-relative colored-patch size prior is used in that case; it is not physical calibration. Pixel traces, original sample crops and failed geometry diagnostics remain reviewable; metric speed stays unavailable. The Feb 2 rerun (`35f569de-55d5-54d6-a96c-cae9b32a5559`) retained 80 samples, three moving fragments and 12 native crops. The live UI showed all three nominal-speed cells as unavailable. Screenshot: `data/skill-motion/fallback-review-proof.jpg`. One fragment includes a stationary interval before movement, so these are not automatically separated collision/release phases.

Validation: 521 tests passed before the final actor-time-scope guard; all eight sequence tests then passed including that guard. Ruff, JavaScript syntax and diff checks passed. Live API runs succeeded, the proposed thrower association was saved and remained excluded from ratings, and image-only motion was verified in the browser. Local API accounting showed 215 cents in conservative estimates plus outstanding legacy reservations across eight completed calls; this is not billed spend.

Outstanding work remains substantial: reviewable continuity between clips, reliable end/throw order, longer delivery-centered processing, release/target evidence, calibration approval and uncertainty, motion/contact phase separation, and validated automatic weight/aim cohorts. Current activity windows are not sweeping duration or force measurements, and strategic plausibility is not confirmed intent.

## October 30: automatic excerpt campaigns and combined candidate review

**Process an excerpt automatically** now surveys overlapping clips and schedules bounded close-ups around detected deliveries. A run admits one vision job at a time across campaigns, shares the existing local API budget, reuses completed jobs, survives process restarts, and pauses on failed jobs or insufficient reservation capacity. Pause/cancel stop future admissions; an already admitted clip can finish. Progress polls without replacing edited shot or position fields. Plans freeze source/game/profile/model/decoder/sequence/candidate versions. Resume rejects changed inputs. There is no automatic retry of an uncertain API request.

Routes: `POST /api/games/{game_id}/skill-campaigns/preview`, `GET/POST /api/games/{game_id}/skill-campaigns`, and revision-checked `POST /api/skill-campaigns/{campaign_id}/control`. The `SkillCampaign` record retains the plan, survey/refinement job references, revision and checksum. Default 120-second runs have five overlapping survey clips and at most four delivery close-ups. Each run is bounded to 30 minutes, 120 survey clips and eight close-ups; finishing a survey does not establish complete game coverage.

Three real campaigns completed 15 survey clips and five automatic close-ups:

| Preview excerpt | Campaign | Survey / close-ups | Consolidated delivery candidates |
| --- | --- | --- | --- |
| Jan 26 Sheet 8, 1800–1920s | `1c5a58cd-81b9-487d-93bb-a4e684eddbd1` | 5 / 1 | Red at 1856–1862s |
| Feb 2 Sheet 3, 1957–2077s | `db6d93d6-2d70-4936-aa0c-48e7d5a36f14` | 5 / 2 | Red at 1996–2002s and 2067.5–2074s |
| Feb 23 Sheet 2, 1988–2108s | `d1221726-392d-49e0-b267-ec91bc9be004` | 5 / 2 | Yellow at 2016–2026s; red at 2062–2068s |

These are five proposed events, not five verified throws or a complete shot count. Two red candidates close together do not establish alternating order, end numbers or standard four-player roles. All calls remain unknown; no deliberate calling-broom activity or usable broom-tip target was established in these runs. Actor proposals remain clip-local and must not be counted as unique people. The ledger contains 28 completed calls and 270 cents of conservative estimates plus legacy reservations, not billed spend; the local cap remains $5.

Consolidation `overlapping-delivery-observations-v2` attaches an unknown-color observation only when it intersects exactly one known-color component. An unknown interval spanning competing components stays separate and visibly ambiguous; it cannot bridge two red throws or red/yellow observations. This removed the overlapping unknown/red duplicate in the Feb 23 excerpt. Same-color interval grouping remains a provisional temporal association.

**Review candidate** prepares one editable shot with supporting frame references from its observations. It prefers an observation with a proposed release and linked thrower, leaves unknown end/order/measurements blank, and never accepts a shot automatically. Competing release timestamps remain visible. Existing operator fields are preserved; excluded reviews stay excluded; multiple active saved reviews require resolution. The 32-reference document bound is respected, with omitted-reference counts reported and complete observations retained in the candidate. The API and UI distinguish unreviewed, saved, excluded and conflicting candidates without treating those counts as verified coverage.

Browser evidence: `data/skill-vision/campaign-review-proof.jpg`. The main server remains at port 8765 with 16 workers, four recording slots and eight-way recording parallelism. It was restarted only after the queue had no running or queued jobs. `.env` is ignored and mode 0600; `.env.example` is staged without a key. Work remains local in the shared dirty worktree.

Next: capture the receiving-house broom before delivery, retain observed targets separately from strategy hypotheses, establish reviewable cross-clip participant continuity and end/order, and connect validated calibration and free-release motion to reports. No validated player weight/aim ratings exist yet.

Validation for this checkpoint: 535 tests passed, including campaign concurrency/restart/budget tests and combined-draft preservation/exclusion/API-save tests. Ruff, JavaScript syntax and diff checks passed. Live browser checks confirmed completed campaigns, the budget banner, Jan 26 saved-field preservation, and the combined Feb 23 red draft with unknown release. Draft inspection did not change saved reviews.

## October 30: anchored pre-delivery broom and call evidence

Each delivery candidate now has **Search pre-delivery call**. The endpoint `POST /api/games/{game_id}/skill-candidates/{candidate_id}/call-evidence` queues both house views over up to 16 seconds before the earliest supporting delivery observation. The receiving end is unresolved in the seed profile, so neither view is automatically treated as the called end. Requests freeze the anchor sequence checksum and delivery index; they must stay inside the game span, within 30 seconds before the anchor, and end no later than the first observed delivery frame. Actual decoded call frames must be strictly earlier. Changed/missing anchors and absent delivery indices are rejected. Repeating the same request reuses its durable jobs.

`pre-delivery-call-evidence-v2` distinguishes a deliberately called line, a visibly pointed stone/region, and a visible broom tip with unknown purpose. The last category records an object location only, without claiming ice contact or intent. The first live version was too restrictive to retain clearly visible broom tips when the holder was cropped out; that evidence remains retained as an earlier analysis. V2 preserves those useful raw observations without promoting them to calls. The schema also separates tactical alternatives and their missing context from the observation-supported call field. Tactical plausibility never fills a shot's call or target automatically.

Each point retains its frame index, source timestamp, actual rounded crop bounds, source pixel/normalized coordinates, original native frame, marked overlay and provenance. Coordinates remain in image space, not calibrated ice coordinates. `GET /api/skill-sequences/{sequence_id}/targets/{index}/{native|overlay}.png` serves the checksummed evidence. The review page displays the unknown call, association limitation, original/marked frame and separate strategy hypotheses. Existing skill-evidence retention protects these assets. No new shot or rating is saved by running a call search.

Automatic campaign version `bounded-skill-excerpt-campaign-v2` adds `max_call_candidates` (default one, maximum four); each selected candidate gets both house views after the survey. Call and sequence method versions are frozen with the plan. Call clips do not reduce the count of deferred delivery close-ups. Both manual and campaign vision jobs now share a global single-running-request rule; local CV jobs can still run concurrently. This prevents temporary $1 reservations for parallel house views from causing avoidable pilot-budget failures.

Real processing completed both views on all three excerpts:

| Excerpt | Pre-delivery interval | Top / bottom call-evidence result IDs | Result |
| --- | --- | --- | --- |
| Jan 26 Sheet 8 | 1840–1856s | `419fa8f6-e5e2-506f-9516-e2a834a6eef8` / `38e2b736-c932-5210-9b9d-510adad7b653` | Two visible broom tips; purposes unknown |
| Feb 2 Sheet 3 | 1980–1996s | `0ff37e4a-adb0-5aa6-be79-341d3caa6e8f` / `f1db5ca0-b550-534d-a1a9-08ffb5ba2a4f` | Two visible broom tips; purposes unknown |
| Feb 23 Sheet 2 | 2000–2016s | `d034588c-fc5b-5f93-90a2-82365fabc5ef` / `eced6d51-1dea-5d72-8ea2-744dfab81568` | Two visible broom tips; purposes unknown |

All six calls remain unknown. Two earlier Jan 26 v1 searches also completed and are preserved. The Jan 26 bottom-house overlay was visually checked: its marker lands on the visible broom head at 1840s, source pixel (765.6, 736.8). That check verifies one image localization, not call intent or general detection accuracy. Browser proof: `data/skill-vision/call-evidence-review-proof.jpg`. The ledger has 36 completed requests with 295 cents in estimates plus legacy reservations, not billed spend; the pilot cap remains $5.

Remaining work: review and link game-local anonymous participants across clips, establish end/order and receiving-end mapping, collect continuous broom/release/stone paths, approve calibration and targets, and validate consistency reports against real reviewed deliveries. Observed broom tips and strategic options alone are insufficient for weight or aim ratings.

Validation at this checkpoint: 542 tests passed, including parallel worker-claim serialization, automatic two-house planning, source/anchor/frame bounds, target image serving, coordinate mapping, unknown-purpose points and strategy/intent separation. Ruff, JavaScript syntax and diff checks passed. Live API verification confirms six retained point observations across the three excerpts and no automatically created shots or rating groups. The running main server includes the serialization change.

## October 30: game-local participant continuity

Skill review documents now retain `participants`: stable anonymous IDs (`skill-player:...`), editable labels, clip-local actor memberships, review status and a basis. Memberships are validated against the current game/source/sequence evidence and included in revision history. One actor cannot belong to two confirmed participants. Confirmed aliases also participate in the existing one-position-per-end validation. A shot/slot cannot reference an absent stable participant. Referenced sequence checksums are now part of the review context, so changed evidence makes saved reports stale.

Report version `shot-execution-v3` and throwing-order proposals resolve only **confirmed** memberships. Proposed/excluded links do not combine clip-local cohorts; a direct reference to an unconfirmed stable participant is excluded from statistics. Existing operator labels still work. Confirmed shot/slot references to different clip actors in the same reviewed participant agree rather than producing a false conflict. Original shot actor references remain intact for provenance. Stable participants are game-local; no cross-game named identity is inferred.

Continuity method `anonymous-shared-evidence-v1` proposes pairs using either throwers attached to the same non-ambiguous consolidated delivery, or body boxes in the identical decoded full-source frame (same source, timestamp and RGB hash). Crop coordinates are mapped into source-image coordinates. The geometric rule requires box IoU at least 0.55 and at least 0.15 separation from competing people in the same pair of sequences. These thresholds are heuristics, not calibrated probabilities. Same-clip pairs, stale observations and already-confirmed links are not resuggested. Conflicting side proposals or separate confirmed memberships remain visible. The two evidence types come from the same footage and are not independent identity evidence. No facial recognition, named lookup or clothing-only cross-game matching is involved.

The UI shows both crops and the supporting event/frame. **Prepare participant link** fills an editable draft, defaults to proposed, and can extend an existing participant without silently merging separate assignments. Activity cards can add observations to the draft; adding a new observation resets confirmation. Users can remove observations, confirm/reject a mapping, edit its label and use it in a shot. Position rows now offer saved anonymous labels alongside custom/end-only labels, so users need not type internal IDs. Changing games clears participant drafts.

The real data produced 11 pair suggestions: five Jan 26, five Feb 2 and one Feb 23. One Jan 26 link was visually reviewed and saved through the UI at skill revision 3: `skill-player:1c5a1fd4-66a1-4eff-8f58-612e0bc57d3a`, **Anonymous player 1**, links A1 from sequences `30b0117b-b433-5d76-8611-e377af4c76f8` and `d09f5605-7b4a-53a0-93f1-730f1d85570a`. Their body crops overlap in the identical decoded frame at 1859.50s and show the same delivery. The basis explicitly records assistant visual review. This confirms local continuity only; no name, team position, end, call or skip role was accepted. The original shot remains proposed and excluded from statistics. The confirmed pair disappears from unresolved suggestions.

Validation: 549 tests passed, including alias-aware report pooling/role conflicts, unreviewed identity exclusions, duplicate memberships/positions, source-frame/crop geometry, ambiguous neighboring boxes, current-game membership and stale sequence checksums. Ruff, JavaScript syntax and diff checks passed. The live UI saved the mapping and displayed the stable label while preserving rating exclusions. Position selection was inspected without saving a position. Screenshot: `data/skill-vision/participant-review-proof.jpg`. No additional paid vision requests were needed.

Next: establish delivery order/end context and continuity across nonoverlapping throws, connect visible broom/release paths to approved calibration, and extend evidence beyond the short preview excerpts. Stable labels alone do not establish throwing roles or valid accuracy cohorts.

## October 30: longer coverage without discarding unchanged evidence

**Extend review coverage** uses `POST /api/games/{game_id}/skills/coverage` with the current game revision and source-span index. It only widens an existing partial/unconfirmed span, validates recording bounds, and records a normal game revision. It does not accept boundaries, change team/end/result interpretation, or rewrite immutable skill results. No-op extensions do not add revisions. Accepted/complete games must use normal boundary review.

Skill review, sequence, motion, call-anchor and campaign checks now recognize a compatible coverage extension only when every revision from the original to current revision is present, each step removes no source time, all non-boundary fields are identical, and the last history entry matches the current game. A shrink followed by expansion is not compatible; missing history, source changes, interpretation edits and retirement remain stale. Source metadata and evidence checksums are still enforced separately. Queued jobs keep their original revision provenance and can finish across an audited extension. This compatibility applies to skill analysis; other catalog reconstruction keeps its own rules.

The three existing source-end timelines were inspected over 0–4000s. None provided end-transition candidates or reliable delivery order near the preview excerpts. Feb 2 has a later paired-board epoch at 2460–2580s (red end 2 cumulative 3; yellow end 1 cumulative 3), but it does not establish the earlier delivery's end/order. Diagnostic snapshots are retained as `data/skill-vision/end-context-{source_id}.txt`.

Jan 26 review game `9e56460a-9aa1-5d98-b53d-d9e9c48a391d` was widened through the live UI from 1800–1920s to 1200–3000s (20–50 minutes), game revision 2. Skill review revision 3, the confirmed anonymous participant, all previous sequences and motion evidence remain non-stale. The game remains partial/proposed with unknown ends and result. Campaign `99a62d69-9c0c-4b93-9a53-e6104f4f314e` is processing 70 survey clips, followed by up to four delivery close-ups and two call searches. At this checkpoint seven survey clips had succeeded and the eighth was running; completion and full shot coverage are not claimed. Browser proof: `data/skill-vision/extended-coverage-proof.jpg`.

The two initial completed vision calls still held $1 reservations because they predated token accounting. Their retained response IDs and complete token usage were reviewed, then reconciled explicitly using reviewed artifact hashes. `reconcile_completed_reservations` has preview/apply modes, leaves uncertain/incomplete attempts reserved, detects changed artifacts, and records each adjustment in `accounting_adjustments`. The two completed legacy reservations changed from 100 cents each to conservative estimates of 1 and 2 cents. This reduced the ledger from 295 to 98 cents before the longer survey, without changing the $5 cap. The audit records that historical checksums were absent; sealing the current reviewed files is not proof of historical cryptographic integrity or actual billed spend.

Validation: 555 tests passed, including monotonic history compatibility, shrink/restore and missing-history rejection, accepted-game and recording-bound checks, revision conflicts, old queued jobs and actor evidence after extension, and idempotent audited accounting reconciliation. Ruff, JavaScript syntax and diff checks passed. The longer survey is live; do not restart its server while a job is running.

### Retained-house context and completed longer survey (October 30)

The January 26 Sheet 8 campaign `99a62d69-9c0c-4b93-9a53-e6104f4f314e`
completed all 70 overlapping survey clips covering source seconds 1200–3000,
four delivery close-ups and two pre-delivery house searches. It produced 23
combined delivery candidates. This is sampled excerpt coverage, not a verified
full game or complete throwing order. Unknown stone colors and consecutive
same-color candidates still require review for missed or mistaken deliveries.
The conservative local vision ledger was $3.16 across 112 completed requests
at this checkpoint, within the unchanged $5 pilot limit; it is not the invoice.

The Player skills page now has an **End and delivery context** section. It runs
local HSV and obstruction analysis on the already retained composite contact
sheets, without downloading video or making another OpenAI call. Analysis is
bounded to 128 sequences, 1200 distinct frames and 30 seconds; oversized work
fails without publishing a truncated result. Source/profile partitions are
kept separate. Frozen sequence checksums, source times, original RGB hashes,
contact-sheet crop coordinates and method versions make results reproducible.
No additional duplicate crop files are stored. Cached results are reused;
new sequences are indicated separately and source/game/method drift is stale.

`retained-survey-inner-house-v2` measures colored handle candidates inside 85%
of the fitted outer-house radius. Full-house diagnostics are retained beside
this deliberately limited region. Stable configurations require distinct
source frames and obstruction checks. Possible clearing proposals require
matching geometry and continuous sampled coverage. Changes failing those
checks appear separately as unresolved, with their reasons and before/after
images. Neither category assigns an end number, score, role or player rating.

The real January 26 pass analyzed 881 distinct frames in 2.567 seconds and
retained 91 stable inner-house layouts. It omitted one unsupported old sequence
and all 42 copies at 19 source timestamps where overlapping decodes had differing
RGB hashes. Those omissions create explicit coverage gaps. Missing/corrupt
retained images abort publication; unsupported geometry is reported as skipped.
A six-candidate-to-zero transition at 2048–2088 seconds is displayed as unresolved
because the fitted ring geometry differs. Visual inspection of the two crops
shows the populated and cleared house, but does not verify the intervening play,
official end number, score or complete delivery order.

The UI analysis/cache action and before/after image endpoints were verified in
the browser. Proof: `data/skill-vision/house-context-review-proof.jpg`. The full
suite passed 565 tests before two final edge-case additions; all 12 focused
house-context tests then passed, including conflicting overlapping decodes,
corrupt assets, cache invalidation, sampling gaps, obstructions, repeated frames,
camera changes and bounded work. Ruff, JavaScript syntax and diff checks passed.
No calibrated weight or aim cohort is available yet; the saved proposed shot
remains excluded. Further work should establish reviewed end/order anchors and
inspect missed-delivery gaps before suggesting lead/second/third/fourth slots.

### Throwing-order constraints and standing deliveries (October 30)

Skill reviews now retain evidence-backed end intervals (`end_windows`) without
changing the game's scorecard. Confirmed intervals cannot overlap in one source,
and each source/end has one confirmed interval. They are checked against the
game span, revision and referenced sequence evidence. The UI can save, edit or
exclude intervals and show possible side, team delivery number and position.

`reviewed-end-subsequence-order-v1` uses forward/backward constraint propagation
over a standard four-player, sixteen-delivery end. Every unobserved throw remains
possible; an alternating partial excerpt is not automatically numbered from one.
Only confirmed shot numbers anchor the sequence. Conflicting anchors, overlapping
candidate intervals, boundary crossings and stale/unconfirmed end intervals
withhold order. A unique result can prepare a new proposed shot; existing saved
shot decisions are preserved. Fourth never establishes skip. Standard four-player
format is explicit; this model does not cover doubles or other delivery orders.
No end interval or official end number has yet been confirmed for the real games.

Same-color observations now flag gaps for inspection, without claiming a missing
throw. When a gap contains sweeping by the other stone color, a focused camera
search can inspect the preceding eight seconds and following four seconds. Its
camera comes from the retained activity box and frozen profile, not clothing/team
color. Legacy sequences without profile provenance are skipped. The normal
versioned, budgeted sequence worker handles these searches.

This exposed an actual detector failure: v3 recognized sweeping but missed an
upright stick delivery. Review of the retained January 26 right-camera frames
around 2210–2218 seconds shows a participant guiding the yellow stone forward
with a stick and the stone continuing independently. The original prompt
required a slide or hand release. `curling-sequence-v4` explicitly includes
standing stick and standing hand deliveries, distinguishes them from cleanup,
and permits an unknown release frame. The dependent call prompt is versioned
`pre-delivery-call-evidence-v3`; older evidence remains intact. Retained-house
analysis supports the unchanged contact-sheet layout of both sequence v3 and v4.

The initial focused v3 sequence `7a615b0e-68b0-5d82-9887-b06093435332`
returned no delivery. The corrected v4 sequence
`d7f3d920-2b06-55ae-a272-1951219a5ea2` (2209–2221 seconds) proposed one yellow
stick delivery with a linked anonymous thrower and unknown exact release/call.
This raised the excerpt to 24 combined candidates and removed the corresponding
same-color gap. These are proposals, not accepted player ratings or identities.
A bounded 15-clip recovery survey, campaign
`2ced04d2-4cd2-4e93-8239-3dd0fd177339`, is processing 2084–2460 seconds using
v4, with no extra close-ups/call searches. The $5 local cap remains unchanged.
At launch, $3.22 was estimated completed spend plus a $1 in-flight reservation.

Validation: the full suite passed 575 tests before the final prompt/legacy-profile
corrections; 44 focused sequence, campaign, house-context, call-target and order
tests then passed. Order tests compare the solver against exhaustive paths and
cover partial excerpts, missing throws, contradictory anchors, stale intervals,
source bounds and sweeping-cue targeting. Browser review verified the real gap
search action and the recovered candidate count. Proof:
`data/skill-vision/throwing-order-review-proof.jpg`. Ruff, JavaScript syntax and
diff checks passed. Weekly account usage was 66% remaining at this checkpoint.

### Delivery-linked perspective motion (October 30)

The corrected 15-clip recovery campaign completed all clips. It refined some
observed delivery intervals but did not add candidates beyond the targeted stick
crop: the Jan 26 excerpt still has 24 combined candidates. Composite resolution
remains a practical limit for distant standing deliveries. The vision ledger now
estimates $3.63 across 129 completed requests, no reservations, under the unchanged
$5 cap. Subsequent motion work uses local decoding/CV and makes no OpenAI calls.

`bounded-camera-motion-job-v5` / `camera-motion-measurements-v4` adds native
perspective-camera motion. **Inspect delivery motion** on a candidate chooses its
camera from the frozen delivery view/actor box, then samples from one second before
to five seconds after the proposed release. When release is unknown it uses the
end of the observed delivery action. The request freezes the sequence/index/hash;
source/game/anchor changes invalidate execution or the saved result. This is an
observation link, not automatic verification of the stone's physical identity.

House-camera metrics retain their explicit nominal-plane assumptions. Perspective
clips never borrow that projection or produce physical speed, weight multiplier
or aim error. They retain image tracks, same-color flags and timing labels before,
across or after the proposed release, or release unknown. In particular, an
"after" label does not certify unswept, collision-free travel. The overhead house
view often shows controlled delivery motion, so the perspective clip is needed
to observe the stone farther down the sheet.

Perspective frames may be up to 1080 pixels per side. The worker retains eleven
evenly selected native frames plus the last frame (at most twelve) rather than
keeping all 160 full-height crops in memory. All samples retain timestamps, original
RGB hashes and detections. Initial raw-HSV experiments incorrectly linked hats and
clothing to stone paths. The current version reuses the existing perspective
handle detector's gray/granite neighborhood and seed-camera bounds, plus the
pinned local YOLOX person model to exclude upper-body color patches. Only person
box geometry is saved here; no face/name recognition is performed. Model
availability/hash is explicit, and missing person filtering is shown in the UI.

Real processed examples:
- Stick delivery: motion `f328a0a6-5bc1-5948-bcc2-dec6c2db4674`, source 2216–2222,
  60 frames, eight image fragments, exact release unknown. Earlier unfiltered
  motion `6dfc9275-679c-5f9e-99c9-2a1c04ac58b7` remains as provenance.
- Sliding delivery: motion `39410bbb-bbc4-5f37-86fb-7027bdac1698`, source 2263–2269,
  60 frames, three fragments. A 13-point red fragment crosses the proposed release
  at 2264; a 44-point red fragment spans 2264.6–2268.9; an unrelated seven-point
  yellow fragment is flagged different-color. The overlay was visually reviewed
  and the longer red path follows the visible stone. The earlier raw-HSV result
  `871323ac-6fcc-524a-a81e-06bedb8fa741` had fourteen fragments.

The UI displays newer motion results first, limits tall overlays to a reviewable
height, and exposes phase labels and original frames. Native screenshot proof:
`data/skill-vision/perspective-motion-review-proof.jpg`. Physical calibration,
reviewed target/call and free-travel/stone association still need verification
before these trajectories enter any weight or aim cohort.

Validation: full suite 577 passing before the final detector refinement; all 12
motion tests passed after it. Coverage includes a 160-frame full-height perspective
clip, bounded image retention, no inappropriate house projection, anchor checksum
invalidation, release-relative labels, upper-person-box exclusion and preserved
house metrics. Ruff, JavaScript syntax and diff checks passed. The real examples
completed successfully using the installed pinned person model. Weekly account
usage was 64% remaining at this checkpoint.

### Reviewed landmark camera calibration (2026-10-30)

`reviewed-plane-landmarks-v1` adds source-bound camera calibration reviews to the
existing optimistic SkillDocument history. Each review pins one motion document,
camera view, original retained image checksum, time interval and explicit meter
coordinate convention. Nominal rules dimensions and site-measured dimensions
remain distinct. Motion, source, game or delivery-anchor changes invalidate the
review; images are verified before use. This first version applies only to tracks
from the pinned motion, without implicit reuse across clips or camera layouts.

The normalized DLT fit requires at least four fit landmarks plus two independent
check landmarks. Coincident, collinear, out-of-image, unstable and horizon-crossing
geometry is withheld. Check points must lie inside the convex support region, and
both fit and check residuals must meet the declared tolerance. Confirming a failed
fit is rejected. Proposed/incomplete maps can be saved for further review. The
preview reports individual landmark residuals as well as the maximum check error.

Only confirmed, validated maps project tracks inside their support polygon and
validity interval, with strictly increasing dense timestamps. Output includes
plane coordinates, fitted velocity, residuals and two-pixel sensitivity. These
are experimental projected handle-center measurements, with uncorrected height,
lens and camera-motion assumptions. They never automatically create player
ratings, weight targets, called lines or accepted shots.

The motion review has a "Calibrate this camera" action. The editor displays the
original image at a bounded size, converts clicks back to native pixels, provides
editable fit/check landmarks and meter coordinates, draws the supported region,
previews diagnostics and saves with revision history. Keyboard entry is available
for every coordinate. Failed saves show an inline explanation. The real Jan26
red-stone motion `39410bbb-bbc4-5f37-86fb-7027bdac1698` was opened successfully at
its original 720×1026 crop size; no physical landmark coordinates were invented or
confirmed for it. Actual-game metric speeds and player cohorts remain unavailable.

Validation: 595 tests passed across the full suite. After adding individual
residual display, all 17 calibration tests passed again; Ruff, JavaScript syntax
and diff checks passed. An isolated browser fixture recovered the known 1.726 m/s
trajectory after confirmation, persisted the calibration, and rejected a changed
check point without altering the saved review. Its synthetic measurements were
never written to the real-game database. Browser proof is in
`data/skill-vision/calibration-synthetic-validation-proof.jpg`; real-game editor
proof is in `data/skill-vision/camera-calibration-review-proof.jpg`.
No additional OpenAI calls were used: 129 completed requests, 363 conservative
estimated cents against the $5 pilot cap. Weekly Codex usage was 63% remaining.

### Called-line direction and real left-camera tracks (2026-10-30)

`reviewed-post-release-line-v1` connects a saved shot, checksum-pinned camera map
and motion fragment to a reviewed pre-shot called line. The operator supplies the
line origin, called broom position and a point physically to the thrower's right
in that calibration's sheet-meter coordinates. The right-side marker makes the
sign invariant under camera mirroring, coordinate rotation and reverse travel.
Curl-left/curl-right maps that signed angle to outside/inside separately; unknown
turn still permits right/left direction but withholds wide/narrow.

The measurement uses 0.15–1 second of observed path within the first 1.5 seconds
after reviewed release. These are conservative pilot window limits, not a
validated performance threshold. It requires review of the delivered-stone
association, free unswept travel without contact, and the pre-release called line.
Missing samples, reversals, unsupported geometry, reverse travel, wrong source,
distant delivery time and conflicting stone color are withheld. Outputs include
short-window fitted angle, observed start/end lateral offset, early/late heading
change, path residual and position sensitivity. A parallel but displaced path can
have zero angle; lateral offset is therefore shown separately. No unswept finish,
sweeping force or weight target is inferred.

`Shot.aim` retains the inputs and evidence. Saving recomputes the numeric result
from the owned motion and calibration, so a manually altered derived value cannot
replace the measured result. A changed calibration requires renewed review.
Proposed aim does not enter statistics even on a confirmed shot. Confirmed shots
with confirmed usable aim enter the existing descriptive line/wide distributions.
`Shot.line_calibration` is separate from the weight reference, and
`shot-execution-v4` keeps both references in its matched cohorts. This preserves
existing weight values when linking camera-derived aim. The UI copies the reviewed
result to the shot draft, preserves other shot decisions and provides an explicit
way to remove the link. Unavailable aim controls stay hidden until a saved shot
and confirmed camera map exist.

Browser validation used an isolated synthetic database. The known trajectory
measured 10.008 degrees right and wide (curl-left); saving retained the independent
0.900 weight ratio and its speed-plane reference. Saving an end interval and a
fresh page load preserved both metrics. Single-shot spread remains unknown.
Proof: `data/skill-vision/aim-report-synthetic-proof.jpg`. No synthetic observations
were inserted into the real game database.

Three additional real Jan26 Sheet8 candidate-motion jobs completed locally:
- `005608b4-e29b-4ea1-8642-6d5d0da7f874`, motion
  `1f75952b-088c-5955-9144-2af41490d018`, 1861–1867 seconds, five fragments.
  The overlay includes an erroneous red-hat path as well as stone paths; a
  24-point red fragment spans 1864.3–1866.6. This is the candidate linked to the
  existing anonymous participant review, not an accepted physical measurement.
- `0b6d4559-ca11-464a-9488-82f3a3c889f2`, motion
  `a0823212-ebf7-5835-94b9-dc831b47b3d0`, 1943–1949 seconds, six fragments.
  Yellow paths span 1943–1945.7 (28 samples) and 1946.8–1948.8 (21 samples).
  Red broom/other-object paths remain visible and unaccepted.
- `fdba89c3-a6ed-469c-95d3-108432ccc4b4`, motion
  `e10536dc-2f53-51aa-840d-53509bc8e193`, 1323.5–1329.5 seconds, two red
  fragments. The longer path has 50 samples from 1324.2–1329.1 and was visually
  inspected on the original reference overlay. It crosses the proposed release;
  free-travel review and metric calibration are still outstanding.

All three have 60 decoded samples in the left perspective camera. Proof for the
long red path: `data/skill-vision/left-camera-motion-review-proof.jpg`. Real data
still has 24 delivery candidates, no confirmed camera map, and no accepted player
accuracy cohort. Upper-person-box filtering reduces false patches but demonstrably
does not eliminate hats and brooms; do not promote all red/yellow paths to stones.

Validation: full suite 611 passing before the final lateral-offset refinement;
57 focused aim, calibration, skill and order tests passed afterward, including
cross-source/time/color rejection and displaced parallel paths. Ruff, JavaScript
syntax, diff checks and browser persistence checks passed. No additional OpenAI
calls were made (129 completed, 363 conservative estimated cents). Weekly Codex
usage was 62% remaining at this checkpoint.

### Retained motion refinement and delivery-path suggestions (2026-10-30)

`camera-motion-measurements-v5` / `bounded-camera-motion-job-v6` adds a tighter
prediction-error gate for dense motion and a smaller displacement bound for the
perspective views. The generic sparse tracker keeps its previous defaults. This
addresses a real failure where a broad association gate let nearby crowded color
patches repeatedly terminate a stone path or let a hat jump onto a background
patch. Ambiguous contacts and missing frames still split tracks; no samples are
invented between fragments.

Delivery-path suggestions use an explicitly limited Wausau seed-camera prior:
known proposed delivery color, sufficient downward image displacement and downward
progress relative to path length. They are labeled "Review first", without a
confidence probability or automatic stone/shot acceptance. Unknown colors and
unsupported layouts abstain. Other image fragments remain available, drawn gray
when a first-review path exists. The aim form prefers first-review paths while
still requiring the separate free-travel, physical association and called-line
reviews. This is not an evaluation establishing general tracking precision.

`retained-motion-refinement-v1` and POST `/api/skill-motion/{id}/refine` recompute
associations from checksum-pinned stored detections. They do not decode media or
call OpenAI. The operation is bounded to 160 samples, 128 candidates per sample,
1080-pixel crop sides, a small overlay budget and a 15-second publication deadline.
It preserves the original run, references the same original images and produces
only a new overlay. Deterministic keys make repeated refinement idempotent. Every
refinement links directly to its original detection run; changed parent evidence
invalidates dependent motion and calibration reviews. The UI opens the completed
result automatically, including cache hits.

Five real Jan26 clips were refined (four measured HTTP calls each took about
0.06 seconds):
- `2eb6eae3-5978-5e1a-9b56-6da649cc9574` from `1f75952b-088c-5955-9144-2af41490d018`:
  the hat-to-background path disappears. First-review red paths have 17 samples at
  1861–1862.6 and 27 at 1864.3–1866.9. Remaining non-stone patches stay gray.
- `33ebc02b-9f06-54da-9a08-937bc674efb9` from `a0823212-ebf7-5835-94b9-dc831b47b3d0`:
  first-review yellow paths retain 28 and 21 samples; red broom/other patches do
  not match the proposed delivered color.
- `645d686d-0de2-555a-a0e0-13c0f7e3a589` from `e10536dc-2f53-51aa-840d-53509bc8e193`:
  one continuous red path now spans 1323.5–1329.1 with 57 samples, versus the prior
  split 6/50-sample fragments.
- `bf47b695-c59d-5e6b-9ae5-d3dee4cab5c8` from `39410bbb-bbc4-5f37-86fb-7027bdac1698`:
  the two red paths remain at 13 and 44 samples; the unrelated yellow fragment is
  no longer associated as a moving track.
- `de646f7d-a284-59c7-8041-50c7e455e05d` from `f328a0a6-5bc1-5948-bcc2-dec6c2db4674`:
  twelve image fragments remain, with the four downward yellow stone fragments
  prioritized (13, 7, 8 and 18 samples). More fragments is not automatically better
  precision: loosening irrelevant crowd interference also retains extra background
  paths, which the suggestion layer displays separately rather than deleting.

Two other recordings were processed after the January-driven adjustments:
- Feb2 Sheet3, job `c3574bc1-be00-40fa-88bc-449026e3a57c`, motion
  `26223b80-a4d2-552b-bee8-3812f5004797`: one red path, 60 samples at 2001–2006.9.
- Feb23 Sheet2, job `bb12d28d-53c9-4b40-abd6-ec55d6d80b2f`, motion
  `f4a1ec72-f165-53a7-9fa1-ae54be192ac6`: one yellow path, 60 samples at 2025–2030.9.

The native overlays for both additional recordings were visually inspected and
follow the visible delivered-stone trajectories. Release, unswept travel and
metric geometry remain unconfirmed. No real player accuracy cohort was accepted.
Proof: `data/skill-vision/refined-delivery-path-review-proof.jpg` and
`data/skill-vision/feb2-delivery-motion-proof.jpg`.

Validation: all 619 tests pass, including dense prediction beside irrelevant
crowds, replacement-patch jumps, explicit seed/color/flow abstention, no decoding
in refinement, image reuse, idempotency and parent-evidence invalidation. Ruff,
JavaScript syntax, diff checks and the real browser refinement/cache-hit workflow
passed. No extra OpenAI requests were used; the ledger remains 129 completed calls
and 363 conservative estimated cents. Weekly Codex usage was 60% remaining.


### Automatic local motion follow-ups (2026-10-30)

`bounded-local-motion-batch-v1` persists a frozen, checksummed plan and per-clip job
references in `skill_motion_batch`. Preview/start endpoints are under
`/api/games/{game_id}/skill-motion-batches`; the start request must match the preview
hash. Repeating a start returns the same batch. Polling updates status without
replacing the operator's shot form. A page-refresh race that could erase a new
preview was fixed. Shared motion request selection now serves individual candidates
and batches. Sample bounds include each clip's separate reference frame.

Real completed batches:
- Jan26 `199dfb17-af56-4079-9e23-3ad5294fd7b9`: two six-second left-camera clips
  beginning 1264.5 and 1364.0 seconds. Motions `008a8fcd-ec0a-5092-a7cf-f2e87804ae66`
  and `1600b9b7-986a-56b0-9ada-b2e4b14f0031` respectively. Native overlays show a
  40-point yellow path for the first and 19/37-point yellow fragments for the second.
  Release is unknown for the first; neither was accepted as a calibrated measurement.
- Feb2 `5bccf7b2-2a55-4b5e-814f-62b51659d296`: one right-camera clip at 2072–2078,
  motion `096dbb56-92ee-50b6-812d-962d91cc8c37`. Fourteen fragments include red
  equipment/shoe patches, some incorrectly prioritized by the simple flow prior.
  This is evidence of remaining false positives, not a clean stone track.
- Feb23 `77f77ed7-374f-4476-aa50-139cda447cdc`: one left-camera clip at
  2066.5–2072.5, motion `a8faf6ff-fa69-5008-a9b8-83cfa549a819`. The native overlay
  has two plausible delivered-stone fragments (29/25 points) and a separate
  14-point broom fragment also suggested for review. Physical association remains
  unconfirmed.

All four jobs succeeded. Native overlays were inspected; no real accuracy cohort
was accepted. Browser verification covered preview, start, live progress,
pause/resume and completed two-clip status. Screenshot:
`data/skill-vision/local-motion-batch-proof.jpg`.

Validation: full suite 625 passing; focused tests cover restart persistence,
idempotency, global single-job admission with concurrent schedulers, optimistic
control revisions, exclusions, changed anchors, failed-job pauses and bounded API
requests. Ruff, JavaScript syntax and diff checks passed. The ledger remains
129 completed requests / 363 conservative estimated cents. Weekly usage was
59% remaining; the broader skill-report goal remains active.


### Camera comparison and reviewed map reuse (2026-10-30)

`retained-camera-registration-v1` compares bounded retained camera frames locally.
ORB matches exclude broadcast header/footer bands; mutual ratio matches, RANSAC,
spatial spread and separately held-out image matches gate a small-drift proposal.
Different source/camera/crop geometry, insufficient texture or spread, disagreement
and large predicted corner movement abstain. This is image alignment, not physical
camera calibration. Moving people and remaining overlays can still bias it.

The camera panel can compare frames before any metric calibration exists. A saved
original confirmed map can then propose landmarks for another clip from the same
recording/camera. The transfer pins the parent checksum, retains its physical
coordinates and dimension basis, and starts proposed. A separate target-landmark
review is required: propagated check points cannot validate themselves. Changing
or removing the parent invalidates reuse; chained transfers are unsupported.
Native crop geometry is checked, and original calibrations serialize without the
new optional transfer field to preserve existing aim-evidence checksums.

After explicit target review and successful geometry checks, derived aim remains
bound to its own motion/calibration hash while reporting under the original
coordinate cohort. `shot-execution-v5` can therefore aggregate independently
reviewed clips in the same physical frame instead of creating an n=1 cohort for
every new calibration ID. Tests verify two synthetic clips combine without losing
their separate evidence and reject changed parent dimensions/reviews.

Real Jan26 comparisons used motion `008a8fcd-ec0a-5092-a7cf-f2e87804ae66` as reference:
- To `1600b9b7-986a-56b0-9ada-b2e4b14f0031`: alignment proposal, 563 fitting
  inliers / 291 held-out matches, held-out p90 residual 1.18 px, predicted maximum
  corner shift 0.69 px.
- To `2eb6eae3-5978-5e1a-9b56-6da649cc9574`: abstained because matches were too
  localized. Do not interpret this as evidence that the camera actually moved.
- To `33ebc02b-9f06-54da-9a08-937bc674efb9`: alignment proposal, 438 fitting
  inliers / 233 held-out matches, p90 residual 1.66 px, corner shift 3.95 px.

These comparisons do not establish centimeter accuracy, measured dimensions or
free stone travel. No real map or player rating was confirmed. Browser proof:
`data/skill-vision/camera-comparison-proof.jpg`. An isolated synthetic database
verified rejection before independent review, successful confirmation afterward,
1.726 m/s recovery and reload persistence; screenshot:
`data/skill-vision/calibration-transfer-synthetic-proof.jpg`.

Validation: full suite 631 passing; Ruff, JS syntax and diff checks passed. No new
OpenAI requests. Weekly usage was 58% remaining.

### Tonight's capture assistance: no tape measure required

The user plans to play on October 30 and cannot measure a calibration rectangle.
Do not depend on an on-ice measurement task. Ask only for readily available sheet,
approximate start time, stone color and throwing position/skip information. Optional
five-second broom-tip indications at the button center and hog-line/center-line
intersection can help label visible landmarks in the fixed rink camera; approximate
times make the moments easy to find. A still called broom before practice shots and
a note of the intended target and whether sweeping occurred help establish intent.

Existing paint can supply nominal geometry, labeled approximate rather than site
surveyed. Two pointed locations alone do not fit and independently validate a
homography. Ring/line extraction, further visible landmarks and scope/uncertainty
checks remain necessary. House-only geometry must not be extrapolated to the
release area without supporting sheet geometry. Normal gameplay footage remains
useful even if no optional calibration actions happen.

### Hierarchical segmentation and user-confirmed end direction (2026-10-30)

`direction-end-delivery-hierarchy-v1` adds game → end-block → delivery proposals
and a JSON endpoint/download at `/api/games/{game_id}/skill-segments`. A layout-
scoped direction rule is revisioned separately in `skill_direction_rule`, keyed by
layout ID and camera crop bounds. Saving it does not change the processing profile
hash or invalidate earlier analyses. The confirmed rule is right-feed delivery
approaching the camera / left-feed far-house skip on odd ends, reversed on even
ends. It applies across the user's games with that camera layout.

A conservative Wausau seed-layout heuristic uses linked controlled-delivery actor
boxes in the far-house region near the earliest observation. Cropped-camera names
or moving-stone sightings alone do not establish origin. Opposing origin claims
abstain. This remains an unvalidated origin proposal, with source frame/activity
references retained. Unknown origins, source-span holes and gaps over 300 seconds
split proposed blocks. Direction parity does not create exact end numbers; reviewed
end/shot anchors and the existing missing-throw-aware order solver supply labels
when supported. Reviewed end/parity conflicts are reported rather than overwritten.

Live results from existing observations:
- Jan26 Sheet8: 24 candidates, two direction blocks. Twelve left/even observations
  at 1260–2034 seconds, then twelve right/odd observations at 2166–2970 seconds.
  Proposed boundary bracket 2034–2166 seconds (33:54–36:06), compatible with the
  earlier unresolved house-clearing evidence. Neither block is claimed complete.
- Feb2 Sheet3: one left/even candidate at 1996–2002, then one right/odd candidate
  at 2067.5–2074. This short excerpt does not establish exact end numbers.
- Feb23 Sheet2: two left/even candidates spanning 2016–2068, within one proposed
  direction block.

The UI displays delivery starts and release proposals, with explicit unknown
stone/action finish times. There is no action-only video export yet: short motion
track termination is not sufficient evidence that a stone stopped. Export snapshots
are in `data/skill-vision/segment-hierarchies/`; browser proof is
`data/skill-vision/segment-hierarchy-proof.jpg`. No facial recognition code was edited.

Validation: 67 focused skill/order/scope/candidate/sequence/calibration tests passed,
then all six hierarchy tests passed after adding the HTTP test. The cases include
cross-view duplication, near-camera continuation, conflicting origins, full-end
throw labels, missing coverage, rule scope and optimistic revision checks. Focused
Ruff and JavaScript syntax checks pass. Whole-tree Ruff currently reports unrelated
in-progress identity/lineup changes; those files were left to the concurrent work.
No additional OpenAI requests were used. The former background goal remains cleared;
this work implements the user's subsequent hierarchy request.
