# Game lineups: who played in each game

**Faces & lineups** groups the faces of people standing on the ice into anonymous players for each game, picks one representative face per player, and links the same face across games. Select any face to see every game that person appears in. Name the people you care about (for example your own team) and leave everyone else anonymous. Once a few people carry a team label, **Teams** lists every game where at least two of them appear, whichever sheet it was on.

This is a local feature the archive owner asked for. Since October 30 2026 it runs automatically after each recording analysis (see [automatic face finding](#automatic-face-finding)); recordings analyzed before that join only when you queue them. Face templates stay in `DATA_DIR/biometrics/`, outside the evidence store, the catalog documents and every export or release. Deleting a game's lineup deletes its face data.

## Setup

Face models are pinned and never downloaded implicitly:

```sh
uv run hogline face-models             # status, paths and licences
uv run hogline face-models --install   # explicit download, size + SHA-256 checked
```

| Model | Role | Licence |
|---|---|---|
| YuNet 2023mar (OpenCV Zoo `48b10fa`) | face detector | MIT |
| ArcFace R50 `w600k_r50.onnx` (InsightFace `buffalo_l`) | preferred embedder | non-commercial research use only |
| SFace 2021dec (OpenCV Zoo `48b10fa`) | fallback embedder | Apache-2.0 |

`--without-arcface` installs only the permissively licensed pair. Lineups record which embedder they used; suggestions only compare lineups made with the same embedder. The pinned YOLOX person model from [person processing](ACTORS.md) is also required. OpenCV runs every model; no new Python dependency.

## Automatic face finding

When a whole-recording analysis finishes, its recording joins a queue (`auto_lineup` table). A scheduler that runs with the workers starts the next waiting recording when a slot is free:

- **Game spans.** Each posted score history becomes one lineup from 20 minutes before its first card to 10 minutes after its last (the same padding as the manual span suggestion); overlapping spans merge. A recording with no score history is covered whole. Spans longer than 3 hours are split into equal pieces.
- **Leaner sampling.** One sample every 2 seconds and at most 96 face thumbnails per 5-minute segment (manual lineups keep 240). The October 30 full-recording run at 1 s sampling used 0.9 worker-seconds per second of video and about 40 MB of face data per hour, mostly thumbnails; automatic runs need roughly 0.55 worker-hours and 18 MB per hour of video (estimates; 2 s sampling is not yet timed on real video).
- **One recording at a time** (three parallel segments), so new analyses keep their workers. Fresh analyses go first; queued older recordings run newest-first.
- **Skipped:** titles containing *junior, youth, little rock, kids* or *school* (no junior events), recordings under 15 minutes, and recordings that already have a lineup.

Every automatic lineup is an ordinary lineup batch: progress is under **Jobs**, it can be paused or cancelled there, and it is labelled *auto* in the app. A paused or failed batch marks the recording *needs attention* without blocking the queue; **Queue again** retries it once its batches are resolved.

Recordings analyzed before this feature are not queued on their own. **Faces & lineups → Automatic face finding** offers to queue all analyzed recordings without faces (with an estimate of worker-hours and storage) or only the recordings in the current archive list. From a recording's Results tab, **Find faces** queues just that recording.

```sh
uv run hogline lineups --automatic              # queue status and backfill estimate
uv run hogline lineups --backfill               # queue every analyzed recording without a lineup
uv run hogline lineups --backfill SOURCE_ID ... # queue specific recordings
```

| Setting | Default | Meaning |
|---|---|---|
| `HOGLINE_AUTO_LINEUPS` | `1` | `0` stops starting new automatic lineups (recordings still join the queue) |
| `HOGLINE_AUTO_LINEUP_SLOTS` | `1` | recordings processed at once (1–4) |
| `HOGLINE_AUTO_LINEUP_PARALLELISM` | `3` | segments per lineup at once (1–8) |
| `HOGLINE_AUTO_LINEUP_INTERVAL` | `2` | seconds between samples (0.5–10) |
| `HOGLINE_AUTO_LINEUP_EXCLUDE` | `junior,youth,little rock,kids,school` | comma-separated title words to skip |

Restart the server after changing them. Without installed face models nothing starts; the page says why.

## Running a lineup by hand

In the app: **Faces & lineups → Find faces**, choose a recording and a span. **Suggest game spans from the scoreboard** offers each posted score history from 20 minutes before its first card to 10 minutes after its last.

```sh
uv run hogline lineup SOURCE_ID --start 1200 --end 7200            # one sample per second
uv run hogline lineup SOURCE_ID --start 1200 --end 7200 --interval 2  # about half the work
uv run hogline lineups                                              # summary and team games
```

A lineup is a batch of source-grid segments (default 5 minutes, up to 3 in parallel) under one worker-time and storage budget, visible in **Jobs**. Each segment decodes both end-camera views, finds people with YOLOX and keeps faces that:

- sit in the head region of a body whose feet are on the ice (body bottom at least 40% down the camera crop, which excludes spectators behind the glass);
- pass YuNet confidence 0.7 and an inter-eye distance of at least 6 source pixels (head regions are upscaled up to 4× before detection).

Each kept face stores its embedding (float16), timing, boxes, quality and clothing colour in a checksummed archive. Up to 240 faces per segment (96 for automatic runs) also keep a 64-pixel aligned thumbnail, chosen by size, frontalness, sharpness and detector confidence, but spread across people: a face is skipped once eight kept faces in the segment already resemble it, so whoever stands nearest a camera cannot take every slot. Lineups planned before October 30 kept the 240 largest faces. When every segment has succeeded, a grouping job runs automatically.

## Grouping

1. **Faces → groups.** Average-linkage clustering of face embeddings (cosine distance 0.55). Two faces in the same camera frame are never placed in one group; a group that would contain them is tightened and, if needed, partitioned.
2. **Groups → players.** Groups whose pooled templates match (cosine ≥ 0.70) and that are almost never seen in the same frame (≤ 1%) are merged. This joins one player's close-up, distant and head-down faces, while two people on the ice together stay apart.
3. **Roles.** `player`: at least 10% of the median size of the eight largest groups, seen across at least a quarter of the span. `other_sheet`: seen almost only by one camera near its frame edge; a player on this sheet walks between both ends, so both cameras see them. `minor`: everything else (hidden by default).

**Regroup** re-runs grouping from stored faces without decoding video; it is refused once a game has names.

### Group faces manually

In a recording's face strip, choose **Group faces**, open its lineup if there is more than one game, and tick the face groups belonging to the same person. Choose **Group selected faces** to combine them into one person card. Enable **Show small or other-sheet groups** to include a less frequent face. A name is optional. You can also select tiles in **Faces** to group people across games, or choose **Same person** on a possible match.

Manual groups override the automatic rule that keeps groups in the same game apart. The original observations, thumbnails and group IDs remain available; both old face links open the combined person. **Undo grouping** is available in the lineup and on that person's face page. Undo removes that manual decision; names and any independent automatic matches still apply. Different confirmed names and explicit separations must be corrected before grouping, and stale selections are rejected. Manual groupings and their undo have revision history.

Automatic **Adjust grouping** is refused while a lineup has manual groups. **Redo face scan** removes that lineup's manual memberships along with its original face data. Groups between other lineups remain intact.

## Representative faces

Each game player gets one portrait, used in the face gallery, the recording's face strip and person pages. Close-up faces embed a little differently from the many distant ones, so the single most typical face is usually small and blurred, while the single largest face is sometimes a frame-edge crop with black fill, a warped alignment, or a stray face. The portrait is therefore the thumbnailed face with the best score of size (eye distance, capped at 30 px), frontalness, sharpness and similarity to the player's template, penalising black frame-edge fill, among faces with template similarity of at least 0.4 (`clear-plausible-face-v1`). On the Feb 23 2025 Sheet 2 recording the eight players' largest faces scored only 0.5–0.7 against their templates; the chosen portraits were visibly sharper than the most typical faces and avoided the edge crops. Lineups grouped before this method show their largest face until regrouped.

## Faces across games

The **Faces** tab shows one face per probable person across all games, most games first. Selecting a face (also from the strip above a recording's results, or **All games with this face** on a game's player) lists every game that person appears in, with match similarity, links to the game, the recording at that moment and YouTube.

Identities are recomputed on read from per-player templates (`cross-game-face-identities-v1`):

- Considered: game players with role `player` and at least 15 faces, plus every named player. Other-sheet and small groups are left out.
- Average-linkage clustering of templates per embedder, linking at the *likely* similarity (ArcFace 0.75, SFace 0.8). It runs inside single-linkage components at that cut, which gives the same clusters at archive scale.
- Automatic matches keep two groups from the same game apart; an explicit manual group can confirm that they are one person. Two different named people and pairs an operator kept apart cannot merge. Conflicting clusters are tightened and, if needed, partitioned greedily.
- Players named as the same person always form one identity; their portrait is the clearest member's.

The person page offers **Name in N games** (one operator link per listed game, plus any *possibly also* matches you tick), **Not this person** for a wrong game (a revisioned separation that later recomputation keeps), and *possibly also* candidates down to the *possible* similarity (ArcFace 0.55), at most one per game. Names typed in the archive search also find recordings where a named person or their team appears.

Development check, October 30 2026: the Feb 23 2025 Sheet 2 recording's faces were split into two halves, each grouped separately as if two games, then linked. All nine players found in both halves linked correctly with no wrong link at thresholds 0.65–0.8; same-person template similarity was 0.92–0.995, the closest other person at most 0.67. This is one recording and one day; cross-day linking remains unvalidated.

## Naming and suggestions

Name a whole face from its person page, or a single game player (optionally with a team and position: lead, second, third, skip, alternate). The name links that game player to a persistent person; every edit is revisioned with author and reason. For unnamed players the gallery suggests up to three named people by template similarity. With ArcFace, **likely** means similarity ≥ 0.75 and at least 15 faces; **possible** means ≥ 0.55. Suggestions never name anyone automatically. **Find more** lists unnamed players in other games that resemble one person.

The game view groups confirmed names by their person-level team label and displays their manually assigned positions. A suggestion also shows the suggested person's team, but neither a suggestion nor a persistent team label proves which side someone played on in this game. Edit an existing person's team under **People**. Positions here are game-level operator labels, not inferred delivery order; rotating positions require finer-grained records.

## Validation, October 29 2026

Development checks, not accuracy rates:

- Mar 17 Sheet 6, 30 minutes at one sample per second: 1,800 samples, 6,258 faces. The eight largest groups were the game's eight players, each spanning the full half hour; a man from the neighbouring sheet was flagged `other_sheet`. One small head-down group also passed the player floor.
- Earlier sparse-frame experiment (retained evidence, 12 recordings): ArcFace R50 kept 90% of near-duplicate same-person pairs at a 1-in-1,000 false-match rate measured on simultaneous non-adjacent games (80% for faces under 7 px between the eyes). Identity templates of 15+ faces from simultaneous non-adjacent games never exceeded 0.73 similarity. Same-day links across draws were reliable; no cross-day link could be confirmed from sparse frames.

## Limits

- Sheet membership is a heuristic; people at the edge of a frame can belong to the next sheet.
- One person can still be split into two groups, and look-alikes in matching caps can merge. Names are operator decisions.
- Caps, brims and lowered heads hide faces; distant faces are small (often 15–20 px wide).
- Cross-day matching is unvalidated: clothing and hats change between days and similarity varies with resolution. Treat suggestions as prompts to look.
- Streams added while live: until YouTube finishes processing an ended stream (`post_live`), its video URL serves only the live edge, so earlier times decode to nothing. Lineups and analyses now refuse such recordings with that reason (October 31: Koski vs Dobbs, Oct 30, Sheet 4); refresh the recording's metadata once YouTube has processed it.
- Faces of children: do not run lineups on junior events. Automatic runs skip titles with the words above, but a title need not say *junior*: use **Skip** on such a recording's face strip (or `POST /api/lineups/automatic/SOURCE_ID/skip`) before it runs, and delete its lineup if it already ran.

## Deletion

**Delete this game's face data** removes the game's names, groups, separations, templates and face archives that no other lineup uses; job history remains. An automatically processed recording is not processed again after its lineup is deleted unless you queue it. **Forget** removes a person and every link to them. Revision history records what was removed but not the face data.
