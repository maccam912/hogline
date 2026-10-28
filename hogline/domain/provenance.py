"""Resolve original acquisition manifests without rewriting retained observations."""

from collections import defaultdict

from sqlalchemy import select

from hogline.adapters.sources import digest
from hogline.db import AnalysisWindow, Evidence, Job, MetadataSnapshot, Scan

VERSION = "evidence-acquisition-manifest-resolution-v1"


def acquisition_references(frames, scans, windows):
    refs = defaultdict(list)

    def add(evidence_id, source_id, sha, kind, identifier):
        if sha:
            refs[evidence_id].append(
                {"source_id": source_id, "metadata_sha256": sha, "kind": kind, "id": identifier}
            )

    for frame in frames:
        add(frame["id"], frame["source_id"], frame.get("source_metadata_sha256"), "frame", frame["id"])
    for scan in scans:
        for sample in scan.get("samples", []):
            add(
                sample["evidence_id"],
                scan["source_id"],
                scan.get("source_metadata_sha256"),
                "scan",
                scan["id"],
            )
    for window in windows:
        for eid in window.get("anchor_evidence_ids", []):
            add(eid, window["source_id"], window.get("source_metadata_sha256"), "window", window["id"])
    return {
        eid: sorted(items, key=lambda r: (r["kind"], r["id"], r["metadata_sha256"]))
        for eid, items in refs.items()
    }


def observation_manifest(observation):
    resolution = observation.get("source_manifest_resolution")
    if resolution is not None:
        return resolution.get("metadata_sha256") if resolution.get("status") == "resolved" else None
    return observation.get("source_metadata_sha256")


def resolve_observation_manifests(session, observations):
    """Return enriched copies. Only a unique, checksum-verified acquisition pin resolves."""
    ids = {o["evidence_id"] for o in observations}
    if not ids:
        return []
    frames = {
        r.id: {**r.document, "id": r.id, "source_id": r.source_id}
        for r in session.scalars(select(Evidence).where(Evidence.id.in_(ids)))
    }
    scans = [
        {
            **r.document,
            "id": r.id,
            "source_id": r.source_id,
            "source_metadata_sha256": j.document.get("source_hash"),
        }
        for r, j in session.execute(select(Scan, Job).join(Job, Scan.job_id == Job.id))
    ]
    windows = [
        {**r.document, "id": r.id, "source_id": r.source_id} for r in session.scalars(select(AnalysisWindow))
    ]
    refs = acquisition_references(frames.values(), scans, windows)
    manifests = {
        (r.source_id, r.sha256): digest(r.document) == r.sha256
        for r in session.scalars(select(MetadataSnapshot))
    }
    return [
        {
            **o,
            "source_manifest_resolution": resolve_manifest(
                o, frames.get(o["evidence_id"]), refs.get(o["evidence_id"], []), manifests
            ),
        }
        for o in observations
    ]


def resolve_manifest(observation, frame, references, manifests):
    sid = observation["source_id"]
    claims = list(references)
    if observation.get("source_metadata_sha256"):
        claims.append(
            {
                "source_id": sid,
                "metadata_sha256": observation["source_metadata_sha256"],
                "kind": "observation",
                "id": observation["id"],
            }
        )
    candidates = {(r["source_id"], r["metadata_sha256"]) for r in claims}
    status, sha = "unlinked", None
    if frame is None:
        status = "missing_evidence"
    elif frame["source_id"] != sid or any(source != sid for source, _ in candidates):
        status = "source_mismatch"
    elif len(candidates) > 1:
        status = "ambiguous"
    elif len(candidates) == 1:
        key = next(iter(candidates))
        if key not in manifests:
            status = "missing_snapshot"
        elif not manifests[key]:
            status = "corrupt_snapshot"
        else:
            status, sha = "resolved", key[1]
    return {"version": VERSION, "status": status, "metadata_sha256": sha, "references": claims}
