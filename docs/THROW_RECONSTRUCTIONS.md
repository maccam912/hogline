# Per-throw reconstructions and simultaneous replay

The player report links to **Replay all throws together**. Each delivery ray also has **Reconstruct this throw**. The replay page supports all players together, player filters, real-time or half/double-speed playback, scrubbing, looping and optional trails. Each throw card opens its source video, a synchronized clean path, and the four-camera video warped onto a flat ice plane. Clips are created on demand; generating all paths does not export 48 videos.

Current local example: <http://127.0.0.1:8765/games/ae81313f-9cc1-549f-b5c6-27f9b416d8a0/reconstructions>. The Oct. 30 Koski–Dobbs game has **48 reconstructed paths, 12 each for Lindsey, Ethan, Angela and Matt**. Their attribution remains the existing assistant estimate. No player name is required by reconstruction.

All shots align at the **observed delivery-tee crossing**, not an assumed release instant. Opposite directions rotate 180 degrees, including the lateral coordinate. Playback retains each shot's measured timing rather than scaling its duration. This gives the requested simultaneous ghost replay without implying a shared physical ice state.

## Evidence and limits

Native delivery points seed stone association in both overhead and perspective cameras. Sampled original frames provide yellow-handle and granite-body proposals; association rejects static stones at arrival, merged clothing and ambiguous nearby candidates. Earlier and later observations constrain a smooth whole-track estimate across missing intervals. Existing reviewed finish descriptions constrain guard association. The specially reviewed one-off retains its original anchors and hashes.

Solid trail sections are camera-supported; dashed sections are inferred. Current results comprise **20 partial tracks, 18 apparent rests, nine contact-uncertain tracks and one reviewed observed rest**. Partial/contact markers fade after the last observation instead of being converted into resting stones. Apparent rests are automatic proposals. A path being generated does not establish a complete or correct track, calibrated accuracy, a target, or a physical probability. The method does not resolve all contact, occlusion or identity ambiguities.

The common plane retains nominal **114 ft / 34.7472 m** tee spacing. Registration uses the existing paired-camera observations with that assumed spacing; it does not independently validate the length. The prior independent distance audit is unchanged. The video warp preserves actual frame content: raised people stretch and hidden pixels are not recovered. Only the clean path estimates hidden motion.

The current implementation requires existing delivery rays and perspective maps for the calibrated 1280×720 layout. Unsupported games or stale reviews are unavailable rather than silently assigned this geometry.

## Generation, persistence and resource bounds

`POST /api/games/{game_id}/reconstructions` accepts optional `candidate_ids`, `include_clip` (default false), and `retry_failed` (default false). The per-throw UI requests its clip. `GET` at the same path is read-only and returns catalog/state/paths. `GET /games/{game_id}/reconstructions` serves the viewer. Artifact routes whitelist manifest, trajectory, observations, geometry and source clip, validate current membership/pins and support MP4 byte ranges.

Jobs use the existing durable queue with kind `throw_reconstruction`. Only one reconstruction runs at a time, leaving other worker slots available. Work is bounded to 50 source seconds, four sampled frames per second, at most 204 frames and 240 worker seconds. Retries are explicit and bounded. Cached repeats reuse results; missing/corrupt outputs can be regenerated. Original media is checksum-verified and frame observations retain source presentation times and pixel hashes. Routine extraction does not retain new PNGs.

The artifact key pins method version (`multi-view-throw-reconstruction-v2`), review hash, ray and view geometry. Atomic publication rechecks those pins and the worker lease. The succeeded job stores the manifest digest; manifests contain output checksums. Stale or corrupt results are withheld. This creates proposals without accepting strict measurements, rewriting reviews or invoking paid vision models.

Exports live under ignored `data/reconstructions/`. Source clips are limited to 32 MiB each; the reconstruction folder is bounded by the lower of 512 MiB and the configured working-video allowance. The existing 2048 MiB evidence-cache limit is unchanged. Eight source clips were ready at verification; others generate when opened. Old method outputs remain retained within the bound. Back up private source/media and artifacts separately from Git.

`scripts/render_throw_overlay.py` exports the cached whole team plus each player's panel to a standalone MP4 and poster. Current outputs are `data/reconstructions/team-review/team-replay.mp4` and `team-replay.png`. See `--help` for arguments. Individual reconstructed playback is interactive in the browser; the downloadable per-throw file is the original source clip.

## Validation, November 1, 2026

- Full local suite: **748 passed**; final focused reconstruction/ray tests: **30 passed** after artifact-route fixes. Focused Ruff, format, JavaScript syntax and diff checks passed.
- Real browser checks: all 48 paths loaded, play/pause, player filtering to 12 throws, scrubbing, speed control, source-video playback, synchronized single path, compiled/playing video warp, and on-demand clip generation. Screenshots are in the reconstruction folder.
- All **200 ready artifact endpoints** passed: 192 JSON responses and eight MP4 byte-range responses. Repeated generation reused all 48 paths. Team MP4 decoded to 1,177 frames / 49.041667 seconds at 24 fps.
- SQLite integrity and foreign keys passed. All original review documents, three original processing-batch documents and original job states were unchanged. The background recording pipeline created an additional automatic repair batch, which failed on duplicate evidence insertion; it is separate from reconstruction, and its evidence/error remains retained. All reconstruction jobs completed successfully.
- Verification receipts, baseline/recovery SQLite backups and endpoint results are under `data/reconstructions/`. Evidence cache was 1153.05 / 2048 MiB; reconstruction artifacts 274.92 / 512 MiB. Vision ledger remained 248 completed calls, three historical reservations and 990 conservative committed cents.

Code: `perception/throw_reconstruction.py`, `orchestration/throw_reconstructions.py`, `review/reconstructions.py`, `review/static/reconstructions.{html,js}`, worker dispatch, player-report links, shared registration in `perception/perspective_rays.py`, and `tests/test_throw_reconstructions.py`.
