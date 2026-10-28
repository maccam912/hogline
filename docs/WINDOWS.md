# Short detailed windows

**Analyze a short window** or `hogline window SOURCE_ID --start 1800 --end 1820 --interval 0.5` enqueues a durable detailed job. The existing local worker claims it with a lease and bounded retries. No game or end record is created automatically.

The decoder seeks once and processes at most 60 source seconds, 240 selected frames, and 120 decoder wall seconds. Sample intervals range from 0.25 to 2 seconds, defaulting to 0.5. This is a minimum separation between actual frames. Original integer PTS and rational timebases come from FFmpeg's [showinfo](https://ffmpeg.org/ffmpeg-filters.html#showinfo); the [select filter](https://ffmpeg.org/ffmpeg-filters.html#select_002c-aselect) chooses frames without replacing timestamps or manufacturing a constant frame rate. Variable-frame-rate gaps and nonzero local start timestamps have integration tests.

The worker-time limit defaults to 120 seconds including media resolution, decoding, detection, and publication. An in-progress media resolver can take up to its existing 60-second timeout to return; decoder ticks check cancellation, leases, and elapsed time. Closing an interrupted decoder kills and reaps the child process. Retained data defaults to 64 MiB per window; PNG anchors and Parquet count against this and the shared cache limit. These limits do not measure downloaded network bytes. Remote seeks may fetch surrounding codec/keyframe data.

## Stored observations and evidence

Each selected frame produces one [Parquet](https://arrow.apache.org/docs/python/parquet/parquet.html) row per overhead view. Explicit column types retain original PTS/timebases, source/manifest/profile/model hashes, source-relative time, YouTube timestamp link, decoded RGB hash, crop dimensions, handle centers/colors/shapes, motion, and large dark regions. The file metadata embeds the pinned job inputs. Coordinates are crop pixels, with no metric calibration or physical A/B mapping.

One SQL window row indexes the file hash, sample bounds, diagnostics, and candidate intervals. It does not add a SQL row for every sampled frame. A bounded rolling image buffer retains the first/last frame plus start/end anchors for up to three configurations: at most eight composite frames and their seed crops. Other images are discarded after analysis. Inspecting unretained pixels or applying a new image model requires decoding this short interval again; raw per-frame pixel measurements remain available in Parquet for later geometry work.

The API serves `GET /api/windows/WINDOW_ID/observations.parquet` only after checking its content hash and path. Releases include window manifests and anchor references; the local evidence/Parquet files need to accompany a backup. Repeated identical successful requests reuse their job. **Reanalyze this window** or CLI `--new-run` creates an explicit fresh run while retaining the old one. Detector, temporal method, profile, and source manifest changes also produce new job identities. Failed attempts publish no partial logical window, although already written content-addressed artifacts may remain in the bounded cache. A failed dense attempt currently decodes its small interval again on retry.

## Experimental persistence checks

`handle-configuration-persistence-v2` requires at least four distinct samples spanning 1.5 seconds. The complete candidate set must match by color and position against both the first and previous samples. Global crop motion, gaps, count/position/color changes, clipped candidates, or large dark regions split the run. An empty detection list is unknown visibility/detection, not an empty-house claim.

The dark-region check uses low-value, modest-saturation connected components larger than 1% of the crop, with a minimum extent. It flags possible obstruction. It is not a person detector: logos or granite clusters may also trigger it, and people can be missed. Its absence does not establish clear ice. These thresholds are experimental and are not calibrated probabilities or measured accuracy rates.

A real January pilot exposed a persistent person’s foot/clothing false positive under v1. The retained frame also contains a large dark foreground region; v2 abstains on such frames. The old proposed run remains visible with an earlier-version label. This is a development regression case, not held-out evidence of accuracy. Persistence alone cannot distinguish every broom, logo, garment, or stone.

Configuration proposals carry exact source times, anchor IDs, and YouTube links. They have no score, winner, physical stone identity, delivery number, or established end phase. Before automatic end scoring, add calibrated footprints, stronger visibility/detection models, direction/clearing and board reconciliation, and independently labeled evaluation across sheets and dates.
