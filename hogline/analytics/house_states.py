"""Immutable cached house-state derivation with exact dense-input lineage."""

import json
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select

from hogline.adapters.sources import digest
from hogline.analytics.tracks import load_track_input, validate_rows
from hogline.db import AnalysisWindow, WindowHouseStates, now
from hogline.reconstruction.house_states import VERSION, house_states

MAX_RESULT_BYTES = 1024**2


def checked_result(row):
    if digest(row.document) != row.sha256:
        raise ValueError("Stored house-state checksum mismatch")
    return {"id": row.id, "sha256": row.sha256, **row.document}


def save_house_states_in_session(session, window, rows):
    validate_rows(window, rows)
    identifier = str(uuid5(NAMESPACE_URL, f"window-house-states:{window['id']}:{VERSION}"))
    existing = session.get(WindowHouseStates, identifier)
    if existing:
        result = checked_result(existing)
        if any(
            result[key] != window[other]
            for key, other in (
                ("source_id", "source_id"),
                ("source_metadata_sha256", "source_metadata_sha256"),
                ("profile_sha256", "profile_sha256"),
                ("input_parquet", "parquet"),
                ("detector_version", "model_version"),
                ("visibility_version", "visibility_version"),
            )
        ):
            raise ValueError("Stored house states disagree with their immutable input")
        return result
    document = {
        "schema_version": "hogline-house-states-v1",
        "window_id": window["id"],
        "created_at": now(),
        "source_id": window["source_id"],
        "source_metadata_sha256": window["source_metadata_sha256"],
        "profile_sha256": window["profile_sha256"],
        "detector_version": window["model_version"],
        "visibility_version": window["visibility_version"],
        "input_parquet": window["parquet"],
        "context_anchor_evidence_ids": window["anchor_evidence_ids"],
        "status": "derived",
        **house_states(rows, window["parquet"]["sha256"]),
    }
    if len(json.dumps(document).encode()) > MAX_RESULT_BYTES:
        document["status"] = "result_limit_exceeded"
        document["diagnostics"]["states_excluded_by_result_limit"] = len(document["states"])
        document["states"], document["transitions"] = [], []
        document["notice"] = "House-state result exceeded one MiB; retained observations remain available."
    row = WindowHouseStates(
        id=identifier, window_id=window["id"], version=VERSION, sha256=digest(document), document=document
    )
    session.add(row)
    session.flush()
    return checked_result(row)


def derive_house_states(db, window_id):
    with db.snapshot_session(write=True) as session:
        row = session.get(AnalysisWindow, window_id)
        if row is None:
            raise ValueError("Detailed window not found")
        window = {**row.document, "id": row.id, "source_id": row.source_id}
        return save_house_states_in_session(session, window, load_track_input(db.settings.data_dir, window))


def house_states_in_session(session, *, full=False):
    output = []
    for row in session.scalars(select(WindowHouseStates).order_by(WindowHouseStates.id)):
        result = checked_result(row)
        if not full:
            result["state_count"] = len(result.pop("states"))
            result["transition_count"] = len(result.pop("transitions"))
        output.append(result)
    return output
