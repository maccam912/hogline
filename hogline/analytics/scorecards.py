"""Checked scorecard observations and selection of the current reader for each source."""

from sqlalchemy import or_, select

from hogline.adapters.sources import digest
from hogline.db import Evidence, FrameObservation, ProcessingBatch
from hogline.perception.scorecard import VERSION


def check_observation(document, frame, parents):
    if digest({k: v for k, v in document.items() if k != "sha256"}) != document.get("sha256"):
        raise ValueError("Scorecard observation checksum mismatch")
    if frame is None or (
        document["source_id"] != frame["source_id"]
        or document["source_image"] != frame["image"]
        or document["source_metadata_sha256"] != frame.get("source_metadata_sha256")
        or any(document[k] != frame[k] for k in ("source_seconds", "pts", "time_base_num", "time_base_den"))
    ):
        raise ValueError("Scorecard observation differs from its original evidence")
    for key in ("parent", "detector"):
        identifier = document.get(f"{key}_observation_id")
        parent = parents.get(identifier)
        if identifier and (
            parent is None
            or parent["evidence_id"] != document["evidence_id"]
            or digest(parent) != document[f"{key}_observation_sha256"]
        ):
            raise ValueError("Scorecard parent observation checksum/reference mismatch")


def scorecard_observations_in_session(session, source_id=None):
    supported = ("wausau-middle-row-plates-v2", VERSION)
    rows = list(
        session.scalars(
            select(FrameObservation)
            .where(or_(*(FrameObservation.model_version.startswith(f"{v}:") for v in supported)))
            .order_by(FrameObservation.created_at, FrameObservation.id)
        )
    )
    complete = {
        u["payload"]["model_version"]
        for b in session.scalars(select(ProcessingBatch).where(ProcessingBatch.state == "completed"))
        if b.document.get("kind") == "scorecard_read"
        for u in b.document["units"]
    }
    latest_versions, ranks = {}, {}
    for row in rows:
        family = row.model_version.split(":", 1)[0]
        rank = (row.model_version in complete, supported.index(family), row.created_at)
        sid = row.document["source_id"]
        if sid not in ranks or rank >= ranks[sid]:
            ranks[sid], latest_versions[sid] = rank, row.model_version
    selected = [
        r
        for r in rows
        if r.model_version == latest_versions[r.document["source_id"]]
        and (source_id is None or r.document["source_id"] == source_id)
    ]
    ids = {r.evidence_id for r in selected}
    ids.update(
        r.document[key]["evidence_id"]
        for r in selected
        for key in ("reference", "fixed_header_reference")
        if r.document.get(key)
    )
    frames = {
        e.id: {**e.document, "source_id": e.source_id}
        for e in session.scalars(select(Evidence).where(Evidence.id.in_(ids)))
    }
    parent_ids = {
        r.document.get(k) for r in selected for k in ("parent_observation_id", "detector_observation_id")
    } - {None}
    parents = {
        r.id: r.document
        for r in session.scalars(select(FrameObservation).where(FrameObservation.id.in_(parent_ids)))
    }
    for row in selected:
        check_observation(row.document, frames.get(row.evidence_id), parents)
        for key in ("reference", "fixed_header_reference"):
            reference = row.document.get(key)
            if reference and (frames.get(reference["evidence_id"], {}).get("image") != reference["image"]):
                raise ValueError("Scorecard header reference differs from original evidence")
    return [r.document for r in selected]


def select_inputs(source_id, broadcast, scorecards):
    preferred = [o for o in scorecards if o["source_id"] == source_id]
    return preferred or [o for o in broadcast if o["source_id"] == source_id]


def parent_observations_in_session(session, observations):
    ids = {o.get(k) for o in observations for k in ("parent_observation_id", "detector_observation_id")} - {
        None
    }
    return [
        r.document
        for r in session.scalars(
            select(FrameObservation).where(FrameObservation.id.in_(ids)).order_by(FrameObservation.id)
        )
    ]
