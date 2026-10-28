import copy

import pytest
from sqlalchemy import select

from hogline.adapters.sources import digest
from hogline.analytics.lineage import validate_references
from hogline.db import Evidence, FrameObservation, Job, MetadataSnapshot, Scan, Source
from hogline.domain.provenance import (
    acquisition_references,
    observation_manifest,
    resolve_manifest,
    resolve_observation_manifests,
)
from hogline.reconstruction.board_events import board_timeline
from tests.test_board_events import run


def test_legacy_observations_use_original_scan_manifest_without_mutation(db, sources):
    source = sources[0]
    readings = run(0, source_id=source["id"], source_metadata_sha256=None)
    readings += run(20, source_id=source["id"], source_metadata_sha256=None, red=((1, 2), (3, 4)))
    with db.session() as session:
        for o in readings:
            session.add(
                Evidence(
                    id=o["evidence_id"], source_id=source["id"], evidence_key=o["evidence_id"], document={}
                )
            )
        session.flush()
        for o in readings:
            session.add(
                FrameObservation(
                    id=o["id"], evidence_id=o["evidence_id"], model_version="reader-1", document=o
                )
            )
        job = Job(job_key="original", document={"source_hash": source["metadata_sha256"]})
        session.add(job)
        session.flush()
        session.add(
            Scan(
                job_id=job.id,
                source_id=source["id"],
                document={"samples": [{"evidence_id": o["evidence_id"]} for o in readings]},
            )
        )
        # Refreshing the source must not change the meaning of these older images.
        row = session.get(Source, source["id"])
        row.document = {**row.document, "metadata_sha256": "new-source-manifest"}
    with db.session() as session:
        resolved = resolve_observation_manifests(session, readings)
        assert {observation_manifest(o) for o in resolved} == {source["metadata_sha256"]}
        assert all(o["source_metadata_sha256"] is None for o in resolved)
        assert all(o["source_manifest_resolution"]["references"][0]["kind"] == "scan" for o in resolved)
        assert len(board_timeline(resolved)["changes"]) == 1
        assert all(
            "source_manifest_resolution" not in r.document for r in session.scalars(select(FrameObservation))
        )
        # A second historical acquisition pin creates real ambiguity, not a latest-wins choice.
        job = Job(job_key="second", document={"source_hash": "second-acquisition"})
        session.add(job)
        session.flush()
        session.add(
            Scan(
                job_id=job.id,
                source_id=source["id"],
                document={"samples": [{"evidence_id": o["evidence_id"]} for o in readings]},
            )
        )
    with db.session() as session:
        resolved = resolve_observation_manifests(session, readings)
        assert {o["source_manifest_resolution"]["status"] for o in resolved} == {"ambiguous"}
        assert board_timeline(resolved)["epochs"] == []
    assert all("source_manifest_resolution" not in o for o in readings)


@pytest.mark.parametrize(
    "case,expected",
    [
        ("missing", "missing_snapshot"),
        ("corrupt", "corrupt_snapshot"),
        ("wrong_source", "source_mismatch"),
        ("unlinked", "unlinked"),
        ("no_frame", "missing_evidence"),
        ("pinned", "resolved"),
    ],
)
def test_resolution_abstains_on_unverifiable_inputs(db, sources, case, expected):
    source = sources[0]
    o = run(0, source_id=source["id"], source_metadata_sha256=None)[0]
    with db.session() as session:
        if case != "no_frame":
            session.add(
                Evidence(
                    id=o["evidence_id"],
                    source_id=source["id"],
                    evidence_key="one",
                    document={
                        "source_metadata_sha256": None
                        if case in ("unlinked", "wrong_source")
                        else source["metadata_sha256"]
                    },
                )
            )
        session.flush()
        if case == "missing":
            session.delete(
                session.scalar(select(MetadataSnapshot).where(MetadataSnapshot.source_id == source["id"]))
            )
        if case == "corrupt":
            m = session.scalar(select(MetadataSnapshot).where(MetadataSnapshot.source_id == source["id"]))
            m.document = {**m.document, "title": "modified"}
        if case == "wrong_source":
            o["source_id"] = sources[1]["id"]
    with db.session() as session:
        resolved = resolve_observation_manifests(session, [o])[0]
        assert resolved["source_manifest_resolution"]["status"] == expected
        assert bool(observation_manifest(resolved)) == (expected == "resolved")


def test_release_recomputes_resolution_and_rejects_forged_epoch_provenance():
    manifest = {"title": "Original acquisition"}
    sha = digest(manifest)
    readings = run(0, source_metadata_sha256=None) + run(
        20, source_metadata_sha256=None, red=((1, 2), (3, 4))
    )
    frames = [{"id": o["evidence_id"], "source_id": "source-1"} for o in readings]
    window = {
        "id": "window",
        "source_id": "source-1",
        "source_metadata_sha256": sha,
        "anchor_evidence_ids": [f["id"] for f in frames],
        "candidates": [],
    }
    refs = acquisition_references(frames, [], [window])
    resolved = [
        {**o, "source_manifest_resolution": resolve_manifest(o, f, refs[f["id"]], {("source-1", sha): True})}
        for o, f in zip(readings, frames, strict=True)
    ]
    payload = {
        "sources": [{"id": "source-1"}],
        "games": [],
        "analysis_windows": [window],
        "processing_scans": [],
        "observations": [],
        "source_boundary_links": [],
        "evidence": frames,
        "source_metadata_snapshots": [{"source_id": "source-1", "sha256": sha, "document": manifest}],
        "broadcast": {"observations": resolved, "board_timeline": board_timeline(resolved)},
    }
    validate_references(payload)
    altered = copy.deepcopy(payload)
    altered["broadcast"]["observations"][0]["source_manifest_resolution"]["references"] = []
    with pytest.raises(ValueError, match="resolution disagrees"):
        validate_references(altered)
    altered = copy.deepcopy(payload)
    altered["broadcast"]["board_timeline"]["epochs"][0]["source_metadata_sha256"] = "current-but-wrong"
    with pytest.raises(ValueError, match="observation provenance"):
        validate_references(altered)
