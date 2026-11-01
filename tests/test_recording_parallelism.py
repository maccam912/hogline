from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from sqlalchemy import select

from hogline.adapters.sources import import_youtube
from hogline.config import Settings
from hogline.db import Database, Evidence, Job, ProcessingBatch, queue_field
from hogline.media.retention import pending_cleanup_query
from hogline.orchestration import worker
from hogline.orchestration.plans import BatchControl, control_batch
from hogline.orchestration.recording import RecordingAnalysisRequest, start_recording_analysis
from tests.test_automatic import media  # noqa: F401


def parallel_database(db):
    return Database(replace(db.settings, workers=8, recording_slots=2, recording_parallelism=8))


def third_source(db):
    return import_youtube(
        db,
        "https://www.youtube.com/watch?v=CCCCCCCCCCC",
        {
            "id": "CCCCCCCCCCC",
            "title": "SHEET 3 | 03-17-26",
            "duration": 180,
        },
    )


def test_eight_workers_share_two_recordings_and_never_admit_a_third(db, sources):
    # Existing queued batches gain the configured parallelism when admitted, without changing work keys.
    batches = [start_recording_analysis(db, s["id"], RecordingAnalysisRequest()) for s in sources]
    third = start_recording_analysis(db, third_source(db)["id"], RecordingAnalysisRequest())
    db = parallel_database(db)
    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(pool.map(lambda _: worker.claim(Database(db.settings)), range(8)))
    assert all(claims)
    assert Counter(j["batch_id"] for j in claims) == {b["id"]: 4 for b in batches}
    assert third["id"] not in {j["batch_id"] for j in claims}
    with db.session() as session:
        assert all(session.get(ProcessingBatch, b["id"]).document["parallelism"] == 8 for b in batches)
        assert session.get(ProcessingBatch, third["id"]).document["parallelism"] == 4


def test_one_recording_can_borrow_all_eight_workers(db, sources):
    db = parallel_database(db)
    first = start_recording_analysis(db, sources[0]["id"], RecordingAnalysisRequest())
    claims = [worker.claim(db) for _ in range(8)]
    assert all(claims) and {j["batch_id"] for j in claims} == {first["id"]}
    assert worker.claim(db) is None


def test_sixteen_workers_share_four_recordings_without_admitting_a_fifth(db):
    db = Database(replace(db.settings, workers=16, recording_slots=4, recording_parallelism=8))
    sources = [
        import_youtube(
            db,
            f"https://www.youtube.com/watch?v={i:011d}",
            {
                "id": f"{i:011d}",
                "title": f"SHEET {i} | 03-17-26",
                "duration": 7200,
            },
        )
        for i in range(1, 6)
    ]
    batches = [start_recording_analysis(db, s["id"], RecordingAnalysisRequest()) for s in sources]
    with ThreadPoolExecutor(max_workers=16) as pool:
        claims = list(pool.map(lambda _: worker.claim(Database(db.settings)), range(16)))
    assert all(claims)
    assert Counter(j["batch_id"] for j in claims) == {b["id"]: 4 for b in batches[:4]}
    assert batches[4]["id"] not in {j["batch_id"] for j in claims}


def test_operator_pause_uses_one_slot_and_peer_keeps_processing(db, sources):
    db = parallel_database(db)
    batches = [start_recording_analysis(db, s["id"], RecordingAnalysisRequest()) for s in sources]
    third = start_recording_analysis(db, third_source(db)["id"], RecordingAnalysisRequest())
    control_batch(db, batches[0]["id"], BatchControl(action="pause"))
    claims = [worker.claim(db) for _ in range(7)]
    assert all(claims) and {j["batch_id"] for j in claims} == {batches[1]["id"]}
    assert worker.claim(db) is None  # Peer has only seven initial survey units.
    assert third["id"] not in {j["batch_id"] for j in claims}


@pytest.mark.usefixtures("media")
def test_cleanup_barrier_drains_peer_protects_its_images_then_admits_next_source(db):
    db = parallel_database(db)
    sources = [
        import_youtube(
            db,
            f"https://www.youtube.com/watch?v={i:011d}",
            {
                "id": f"{i:011d}",
                "title": f"SHEET {i} | 03-17-26",
                "duration": 180,
            },
        )
        for i in (1, 2, 3)
    ]
    batches = [
        start_recording_analysis(db, s["id"], RecordingAnalysisRequest(detail_windows=1)) for s in sources
    ]
    claims = [worker.claim(db), worker.claim(db)]
    first = next(j for j in claims if j["batch_id"] == batches[0]["id"])
    peer = next(j for j in claims if j["batch_id"] == batches[1]["id"])
    worker.execute(db, first)
    for _ in range(30):
        outcome = worker.run_one(db)
        with db.session() as session:
            a = session.get(ProcessingBatch, batches[0]["id"])
            if a.state == "completed":
                assert not a.document.get("image_cleanup")
                break
        assert outcome and outcome["state"] == "succeeded"
    else:
        pytest.fail("First recording did not finish")
    assert worker.claim(db) is None  # A completed source cannot be replaced before safe cleanup.
    worker.execute(db, peer)
    with db.session() as session:
        peer_paths = [
            f.document["image"]["path"]
            for f in session.scalars(select(Evidence).where(Evidence.source_id == sources[1]["id"]))
        ]
    resumed = Database(db.settings)
    saw_third = False
    for _ in range(100):
        job = worker.claim(resumed)
        if job is None:
            break
        if job["batch_id"] == batches[2]["id"]:
            saw_third = True
            with db.session() as session:
                assert session.get(ProcessingBatch, batches[0]["id"]).document["image_cleanup"]
            assert all((db.settings.data_dir / p).is_file() for p in peer_paths)
        worker.execute(resumed, job)
    else:
        pytest.fail("Two-slot queue did not terminate")
    assert saw_third
    with db.session() as session:
        assert all(session.get(ProcessingBatch, b["id"]).state == "completed" for b in batches)
        assert all(session.get(ProcessingBatch, b["id"]).document["image_cleanup"] for b in batches)


def test_recording_scheduling_configuration_is_bounded_and_loaded_from_environment(db, monkeypatch):
    monkeypatch.setenv("HOGLINE_RECORDING_SLOTS", "2")
    monkeypatch.setenv("HOGLINE_RECORDING_PARALLELISM", "8")
    settings = Settings.load(db.settings.data_dir)
    assert settings.recording_slots == 2 and settings.recording_parallelism == 8
    for values in ({"recording_slots": 0}, {"recording_slots": 5}, {"recording_parallelism": 9}):
        with pytest.raises(ValueError):
            replace(settings, **values)


def test_existing_catalog_gains_queue_indexes_and_sqlite_uses_them(db):
    with db.engine.begin() as connection:
        connection.exec_driver_sql("DROP INDEX job_state_owner")
        connection.exec_driver_sql("DROP INDEX recording_cleanup")
    migrated = Database(db.settings)
    owner_query = select(Job.id).where(
        Job.state == "queued",
        queue_field(Job, "batch_id") == "example",
        Job.attempts < 3,
    )
    cleanup_query = pending_cleanup_query().with_only_columns(ProcessingBatch.id).limit(1)
    with migrated.engine.connect() as connection:
        for query, index in [(owner_query, "job_state_owner"), (cleanup_query, "recording_cleanup")]:
            sql = str(query.compile(dialect=migrated.engine.dialect, compile_kwargs={"literal_binds": True}))
            plan = connection.exec_driver_sql("EXPLAIN QUERY PLAN " + sql).fetchall()
            assert any(index in row[-1] for row in plan), plan
