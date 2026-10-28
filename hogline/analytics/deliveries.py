"""Cached perspective release hypotheses with exact person/object lineage."""

import json
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select

from hogline.adapters.sources import digest
from hogline.analytics.actors import load_actor_input, save_actors_in_session
from hogline.db import AnalysisWindow, WindowDeliveries, now
from hogline.perception.perspective_stones import perspective_handle_tracks
from hogline.reconstruction.deliveries import VERSION, release_associations

MAX_RESULT_BYTES = 2 * 1024**2


def checked_delivery_result(row):
    if digest(row.document) != row.sha256:
        raise ValueError("Stored release-association checksum mismatch")
    return {"id": row.id, "sha256": row.sha256, **row.document}


def save_deliveries_in_session(session, window, actors, rows):
    identifier = str(uuid5(NAMESPACE_URL, f"window-deliveries:{window['id']}:{actors['sha256']}:{VERSION}"))
    existing = session.get(WindowDeliveries, identifier)
    if existing:
        result = checked_delivery_result(existing)
        if (
            result["input_perspective"] != window["actor_input"]
            or result["actor_result_sha256"] != actors["sha256"]
        ):
            raise ValueError("Stored release associations disagree with their immutable input")
        return result
    available = window["actor_input"]["status"] == "available" and any(
        r.get("perspective_handles") for r in rows
    )
    handles = (
        perspective_handle_tracks(rows, window["actor_input"]["parquet"]["sha256"])
        if available
        else {"tracks": []}
    )
    result = (
        release_associations(
            rows, actors["tracks"], handles["tracks"], window["actor_input"]["parquet"]["sha256"]
        )
        if available
        else {
            "version": VERSION,
            "candidates": [],
            "uncertain_motion": [],
            "diagnostics": {},
            "notice": (
                "Perspective release features are unavailable for this window; explicit reanalysis is needed."
            ),
        }
    )
    document = {
        "schema_version": "hogline-release-associations-v1",
        "created_at": now(),
        "window_id": window["id"],
        "source_id": window["source_id"],
        "source_metadata_sha256": window["source_metadata_sha256"],
        "profile_sha256": window["profile_sha256"],
        "actor_result_id": actors["id"],
        "actor_result_sha256": actors["sha256"],
        "input_perspective": window["actor_input"],
        "context_anchor_evidence_ids": window["anchor_evidence_ids"],
        "status": "derived" if available else "unavailable",
        "perspective_handle_tracks": handles["tracks"],
        **result,
    }
    if len(json.dumps(document).encode()) > MAX_RESULT_BYTES:
        document["status"] = "result_limit_exceeded"
        document["diagnostics"]["handle_tracks_excluded_by_result_limit"] = len(
            document["perspective_handle_tracks"]
        )
        document["candidates"], document["uncertain_motion"], document["perspective_handle_tracks"] = (
            [],
            [],
            [],
        )
        document["notice"] = (
            "Release analysis exceeded two MiB; retained perspective observations remain available."
        )
    row = WindowDeliveries(
        id=identifier,
        window_id=window["id"],
        actor_result_id=actors["id"],
        version=VERSION,
        sha256=digest(document),
        document=document,
    )
    session.add(row)
    session.flush()
    return checked_delivery_result(row)


def derive_window_deliveries(db, window_id):
    with db.snapshot_session(write=True) as session:
        row = session.get(AnalysisWindow, window_id)
        if row is None:
            raise ValueError("Detailed window not found")
        window = {**row.document, "id": row.id, "source_id": row.source_id}
        rows = load_actor_input(db.settings.data_dir, window)
        actors = save_actors_in_session(session, window, rows)
        return save_deliveries_in_session(session, window, actors, rows)


def deliveries_in_session(session, *, full=False):
    output = []
    for row in session.scalars(select(WindowDeliveries).order_by(WindowDeliveries.id)):
        result = checked_delivery_result(row)
        if not full:
            result["candidate_count"] = len(result.pop("candidates"))
            result["uncertain_motion_count"] = len(result.pop("uncertain_motion"))
            result["handle_track_count"] = len(result.pop("perspective_handle_tracks"))
        output.append(result)
    return output
