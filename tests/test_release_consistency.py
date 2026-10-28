from concurrent.futures import ThreadPoolExecutor
from threading import Event, get_ident

import pytest
from PIL import Image
from sqlalchemy import event, func, select

from hogline.adapters.sources import digest
from hogline.analytics import exports
from hogline.db import Evidence, FrameObservation, Job, MetadataSnapshot, Release, Scan
from hogline.domain.catalog import catalog_in_session, save_game, save_in_session
from hogline.domain.models import Game, GameEdit
from hogline.media.frames import observe
from hogline.perception.pipeline import analyze_frame, observations_in_session


def frame_document(db, source, seconds):
    frame, _ = observe(
        Image.new("RGB", (320, 180), "white"),
        {"pts": seconds * 1000, "time_base_num": 1, "time_base_den": 1000},
        source,
        db.settings.data_dir,
    )
    return frame


def publish_frame(session, db, source, document):
    row = Evidence(source_id=source["id"], evidence_key=document["evidence_key"], document=document)
    session.add(row)
    session.flush()
    analyze_frame(session, row, db.settings.data_dir)
    return row.id


def test_release_and_revision_publish_in_one_snapshot(db, sources, monkeypatch):
    source = sources[0]
    with db.session() as session:
        old_id = publish_frame(session, db, source, frame_document(db, source, 1))
    game = Game(
        sheet=5,
        label="Before concurrent review",
        acceptance="human_confirmed",
        spans=[{"source_id": source["id"], "start_seconds": 0, "end_seconds": 10}],
    )
    save_game(db, GameEdit(expected_revision=0, author="Test reviewer", reason="Initial review", game=game))
    new_document = frame_document(db, source, 2)
    captured, proceed, writer_attempted = Event(), Event(), Event()
    real_catalog = exports.catalog_in_session

    def paused_catalog(session):
        data = real_catalog(session)
        captured.set()
        assert proceed.wait(10), "Test did not release the export"
        return data

    monkeypatch.setattr(exports, "catalog_in_session", paused_catalog)
    writer_thread = []

    def before_execute(conn, cursor, statement, parameters, context, executemany):
        if get_ident() in writer_thread and statement.startswith("INSERT INTO evidence_frame"):
            writer_attempted.set()

    event.listen(db.engine, "before_cursor_execute", before_execute)

    def write_concurrently():
        writer_thread.append(get_ident())
        with db.session() as session:
            new_id = publish_frame(session, db, source, new_document)
            revised = game.model_copy(update={"label": "After concurrent review"})
            save_in_session(
                session,
                GameEdit(expected_revision=1, author="Test reviewer", reason="Correction", game=revised),
            )
        return new_id

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            release_future = pool.submit(exports.snapshot, db)
            assert captured.wait(10)
            writer_future = pool.submit(write_concurrently)
            try:
                assert writer_attempted.wait(10)
                assert not writer_future.done()  # BEGIN IMMEDIATE reserved this publication first.
            finally:
                proceed.set()
            release = release_future.result(timeout=10)
            new_id = writer_future.result(timeout=10)
    finally:
        event.remove(db.engine, "before_cursor_execute", before_execute)
        proceed.set()
    assert {f["id"] for f in release["evidence"]} == {old_id}
    assert {o["evidence_id"] for o in release["observations"]} == {old_id}
    assert release["games"][0]["label"] == "Before concurrent review"
    assert release["interpretation_revisions"] == {game.id: 1}
    following = exports.snapshot(db)
    assert {f["id"] for f in following["evidence"]} == {old_id, new_id}
    assert {o["evidence_id"] for o in following["observations"]} == {old_id, new_id}
    assert following["interpretation_revisions"] == {game.id: 2}
    assert following["games"][0]["label"] == "After concurrent review"
    with db.session() as session:
        stored = session.get(Release, release["id"])
        assert stored.sha256 == digest(stored.document) == release["sha256"]
        assert stored.document["interpretation_revisions"] == {game.id: 1}


def test_read_snapshot_keeps_one_view_while_wal_writer_commits(db, sources):
    source = sources[0]
    with db.session() as session:
        old_id = publish_frame(session, db, source, frame_document(db, source, 1))
    new_document = frame_document(db, source, 2)

    def write():
        with db.session() as session:
            return publish_frame(session, db, source, new_document)

    with db.snapshot_session() as session:
        data = catalog_in_session(session)
        with ThreadPoolExecutor(max_workers=1) as pool:
            new_id = pool.submit(write).result(timeout=10)
        assert {e["id"] for e in data["evidence"]} == {old_id}
        assert {o["evidence_id"] for o in observations_in_session(session)} == {old_id}
    assert {o["evidence_id"] for o in exports.snapshot(db)["observations"]} == {old_id, new_id}


def test_orphan_observation_aborts_release(db, sources):
    with db.session() as session:
        publish_frame(session, db, sources[0], frame_document(db, sources[0], 1))
    with db.session() as session:
        row = session.scalar(select(FrameObservation))
        row.document = {**row.document, "evidence_id": "missing-frame"}
    with pytest.raises(ValueError, match="observation references missing"):
        exports.snapshot(db)
    with db.session() as session:
        assert session.scalar(select(func.count()).select_from(Release)) == 0


def test_corrupt_metadata_snapshot_aborts_release(db, sources):
    with db.session() as session:
        row = session.scalar(select(MetadataSnapshot))
        row.document = {**row.document, "title": "Corrupted after import"}
    with pytest.raises(ValueError, match="checksum mismatch"):
        exports.snapshot(db)
    with db.session() as session:
        assert session.scalar(select(func.count()).select_from(Release)) == 0


def test_legacy_manifest_lineage_uses_acquisition_not_current_source(db, sources):
    source = sources[0]
    with db.session() as session:
        unpinned = Evidence(source_id=source["id"], evidence_key="legacy-one", document={})
        scanned = Evidence(source_id=source["id"], evidence_key="legacy-two", document={})
        session.add_all([unpinned, scanned])
        session.flush()
        unpinned_id, scanned_id = unpinned.id, scanned.id
        job = Job(job_key="legacy-job", state="succeeded", document={"source_hash": "older-manifest"})
        session.add(job)
        session.flush()
        session.add(
            Scan(
                job_id=job.id,
                source_id=source["id"],
                document={
                    "request": {"start_seconds": 0, "end_seconds": 2, "interval_seconds": 1},
                    "samples": [{"evidence_id": scanned_id, "seconds": 1, "state": "unknown"}],
                },
            )
        )
    release = exports.snapshot(db)
    assert release["schema_version"] == "hogline-archive-v2"
    assert release["evidence_manifest_refs"][unpinned_id] == []
    assert release["evidence_manifest_refs"][scanned_id] == [
        {"source_id": source["id"], "metadata_sha256": "older-manifest"}
    ]
    assert release["lineage_gaps"]["unpinned_evidence_ids"] == [unpinned_id]
    assert release["lineage_gaps"]["missing_metadata_snapshots"] == [
        {"source_id": source["id"], "metadata_sha256": "older-manifest"}
    ]
    assert {r["sha256"] for r in release["source_metadata_snapshots"]} == {
        s["metadata_sha256"] for s in sources
    }
