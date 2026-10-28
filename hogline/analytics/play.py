"""Bounded play-cue derivation from verified retained dense observations, without decoding video."""

from uuid import NAMESPACE_URL, uuid5

from hogline.adapters.sources import digest
from hogline.analytics.dense import SCHEMA_VERSION
from hogline.analytics.tracks import load_track_input
from hogline.reconstruction.play import VERSION, focused_play

MAX_WINDOWS = 128


def play_in_windows(root, windows):
    results, exclusions = [], []
    if len(windows) > MAX_WINDOWS:
        raise ValueError("Play reconstruction is limited to 128 windows; select a smaller source/game span")
    for window in sorted(windows, key=lambda w: w["id"]):
        if window["parquet"]["schema"] != SCHEMA_VERSION:
            exclusions.append({"window_id": window["id"], "reason": "older_dense_schema_requires_reanalysis"})
            continue
        rows = load_track_input(root, window)
        document = {
            "id": str(uuid5(NAMESPACE_URL, f"{VERSION}:{window['id']}:{window['parquet']['sha256']}")),
            "window_id": window["id"],
            "source_id": window["source_id"],
            "source_metadata_sha256": window["source_metadata_sha256"],
            "profile_sha256": window["profile_sha256"],
            "detector_version": window["model_version"],
            "visibility_version": window["visibility_version"],
            "input_parquet": window["parquet"],
            "context_anchor_evidence_ids": window["anchor_evidence_ids"],
            "dense_coverage": {
                "start_seconds": window["observed_first_seconds"],
                "last_sample_seconds": window["observed_last_seconds"],
                "interval_seconds": window["request"]["interval_seconds"],
            },
            "status": "derived",
            **focused_play(rows, window["parquet"]["sha256"]),
        }
        results.append({"sha256": digest(document), **document})
    return {"results": results, "exclusions": exclusions}
