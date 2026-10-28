"""Release input manifests and reference integrity, independent of score acceptance."""

from collections import defaultdict

from sqlalchemy import select

from hogline.adapters.sources import digest
from hogline.db import MetadataSnapshot


def source_lineage(session, data):
    refs = defaultdict(set)
    required = {(s["id"], s["metadata_sha256"]) for s in data["sources"]}
    required.update((w["source_id"], w["source_metadata_sha256"]) for w in data["analysis_windows"])
    for frame in data["evidence"]:
        if frame.get("source_metadata_sha256"):
            refs[frame["id"]].add((frame["source_id"], frame["source_metadata_sha256"]))
    for scan in data["scans"]:
        source_ref = (scan["source_id"], scan["source_metadata_sha256"])
        required.add(source_ref)
        for sample in scan.get("samples", []):
            refs[sample["evidence_id"]].add(source_ref)
    for versions in refs.values():
        required.update(versions)
    rows = list(
        session.scalars(
            select(MetadataSnapshot).where(MetadataSnapshot.source_id.in_([s["id"] for s in data["sources"]]))
        )
    )
    manifests = []
    present = set()
    for row in rows:
        if digest(row.document) != row.sha256:
            raise ValueError("Source metadata snapshot checksum mismatch; repair it before exporting")
        present.add((row.source_id, row.sha256))
        manifests.append(
            {
                "id": row.id,
                "source_id": row.source_id,
                "sha256": row.sha256,
                "created_at": row.created_at,
                "document": row.document,
            }
        )
    return {
        "source_metadata_snapshots": manifests,
        "evidence_manifest_refs": {
            f["id"]: [
                {"source_id": source_id, "metadata_sha256": sha} for source_id, sha in sorted(refs[f["id"]])
            ]
            for f in data["evidence"]
        },
        "lineage_gaps": {
            "unpinned_evidence_ids": [f["id"] for f in data["evidence"] if not refs[f["id"]]],
            "missing_metadata_snapshots": [
                {"source_id": source_id, "metadata_sha256": sha}
                for source_id, sha in sorted(required - present)
            ],
        },
    }


def validate_references(payload):
    sources = {s["id"] for s in payload["sources"]}
    evidence = {e["id"]: e for e in payload["evidence"]}

    def require(ids, scope, kind):
        if any(i not in scope for i in ids):
            raise ValueError(f"Release {kind} references missing data; publication was rolled back")

    require([e["source_id"] for e in evidence.values()], sources, "evidence")
    for window in payload["analysis_windows"]:
        require([window["source_id"]], sources, "analysis window")
        require(window["anchor_evidence_ids"], evidence, "window anchor")
        require(
            [e for c in window["candidates"] for e in c["evidence_ids"]],
            set(window["anchor_evidence_ids"]),
            "window candidate",
        )
        if any(evidence[e]["source_id"] != window["source_id"] for e in window["anchor_evidence_ids"]):
            raise ValueError("Release window anchor/source mismatch; publication was rolled back")
    for scan in payload["processing_scans"]:
        require([scan["source_id"]], sources, "processing scan")
        require([s["evidence_id"] for s in scan.get("samples", [])], evidence, "processing sample")
        if any(evidence[s["evidence_id"]]["source_id"] != scan["source_id"] for s in scan.get("samples", [])):
            raise ValueError("Release processing sample/source mismatch; publication was rolled back")
    for game in payload["games"]:
        require([s["source_id"] for s in game["spans"]], sources, "game span")
        require(
            [i for e in game["ends"] for i in e["evidence_ids"]] + game["result"]["evidence_ids"],
            evidence,
            "score",
        )
    for observation in [
        *payload["observations"],
        *payload["broadcast"]["observations"],
        *payload.get("board_locations", []),
    ]:
        require([observation["evidence_id"]], evidence, "observation")
        if (
            observation.get("source_id")
            and observation["source_id"] != evidence[observation["evidence_id"]]["source_id"]
        ):
            raise ValueError("Release observation/source mismatch; publication was rolled back")
    for proposal in payload["source_boundary_links"]:
        require([proposal["left_source_id"], proposal["right_source_id"]], sources, "recording link")
        require(
            proposal["left"]["evidence_ids"] + proposal["right"]["evidence_ids"], evidence, "recording link"
        )
