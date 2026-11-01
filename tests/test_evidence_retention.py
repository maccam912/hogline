import copy
import hashlib
import multiprocessing
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select

from hogline.adapters.sources import digest, import_youtube
from hogline.analytics.end_timeline import source_end_timeline
from hogline.db import BoardLabels, Database, Evidence, FrameObservation, Job, ProcessingBatch, Source
from hogline.media.frames import (
    cache_size,
    observe,
    read_image,
    read_image_bytes,
    save_image,
    share_cache_usage,
)
from hogline.media.retention import POLICY, cleanup_completed
from hogline.orchestration import worker
from hogline.orchestration.recording import RecordingAnalysisRequest, start_recording_analysis
from hogline.review.app import create_app
from tests.test_automatic import media  # noqa: F401


def frame(db, source, second):
    image = Image.fromarray(np.random.default_rng(second).integers(0, 255, (90, 160, 3), dtype=np.uint8))
    doc, _ = observe(
        image, {"pts": second, "time_base_num": 1, "time_base_den": 1}, source, db.settings.data_dir
    )
    doc["id"] = digest(doc["evidence_key"])
    with db.session() as session:
        session.add(
            Evidence(id=doc["id"], source_id=source["id"], evidence_key=doc["evidence_key"], document=doc)
        )
    return image, doc


def test_derived_crops_match_original_pixels_and_checksums_without_duplicate_files(db, sources):
    image, doc = frame(db, sources[0], 13)
    assert len(list((db.settings.data_dir / "evidence").rglob("*.png"))) == 1
    for crop in doc["crops"].values():
        expected = image.crop(crop["pixel_bounds"])
        assert read_image(db.settings.data_dir, crop).tobytes() == expected.tobytes()
        assert hashlib.sha256(read_image_bytes(db.settings.data_dir, crop)).hexdigest() == crop["sha256"]
        assert not (db.settings.data_dir / crop["path"]).is_file()
    client = TestClient(create_app(db.settings))
    response = client.get(f"/api/evidence/{doc['id']}/house_top")
    assert response.status_code == 200 and response.headers["content-type"] == "image/png"
    crop = copy.deepcopy(doc["crops"]["house_top"])
    crop["derived_from"]["path"] = "../escape.png"
    with pytest.raises(ValueError, match="outside"):
        read_image(db.settings.data_dir, crop)
    (db.settings.data_dir / doc["image"]["path"]).write_bytes(b"broken")
    with pytest.raises(ValueError, match="corrupt"):
        read_image(db.settings.data_dir, doc["crops"]["house_top"])
    assert client.get(f"/api/evidence/{doc['id']}/house_top").status_code == 410


def test_retention_preserves_observations_labels_live_work_and_resets_shared_counter(db, sources):
    docs = [frame(db, sources[0], i)[1] for i in range(7)]
    other = frame(db, sources[1], 21)[1]
    orphan = save_image(Image.new("RGB", (32, 32), "purple"), db.settings.data_dir)
    snapshot = copy.deepcopy(docs[1])
    with db.session() as session:
        session.add(BoardLabels(id=docs[1]["id"], document={}))
        session.add(
            FrameObservation(
                id="measured", evidence_id=docs[2]["id"], model_version="test", document={"measurement": 42}
            )
        )
        done = ProcessingBatch(
            state="completed",
            document={
                "storage_policy": POLICY,
                "request": {"source_ids": [sources[0]["id"]]},
                "analysis_result": {"test": 1},
            },
        )
        session.add(done)
        session.add(ProcessingBatch(state="active", document={"request": {"source_ids": [sources[1]["id"]]}}))
        session.add(Job(state="running", job_key="fixture", document={}))
    counter = multiprocessing.get_context("spawn").Value("q", 99999999)
    share_cache_usage(db.settings.data_dir, counter)
    try:
        with db.session() as session:
            assert cleanup_completed(session, db.settings.data_dir, review_bytes=0) is None
            job = session.scalar(select(Job))
            job.state = "succeeded"
        with db.session() as session:
            result = cleanup_completed(session, db.settings.data_dir, review_bytes=0)
            assert result["released_files"] >= 5
        assert counter.value == cache_size(db.settings.data_dir)
        assert (db.settings.data_dir / docs[1]["image"]["path"]).is_file()  # Independent human label.
        assert (db.settings.data_dir / other["image"]["path"]).is_file()  # Another source still needs it.
        assert not (db.settings.data_dir / docs[2]["image"]["path"]).exists()
        assert not (db.settings.data_dir / orphan["path"]).exists()
        with db.session() as session:
            assert session.get(Evidence, docs[1]["id"]).document == snapshot
            assert session.scalar(select(FrameObservation)).document["measurement"] == 42
            assert session.get(ProcessingBatch, done.id).document["analysis_result"] == {"test": 1}
    finally:
        share_cache_usage(db.settings.data_dir, None)


@pytest.mark.usefixtures("media")
def test_two_recordings_finish_all_stages_in_order_across_restart_and_release_cache(db):
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
        for i in (1, 2)
    ]
    batches = [
        start_recording_analysis(db, s["id"], RecordingAnalysisRequest(detail_windows=1)) for s in sources
    ]
    # Multiple workers cannot start multiple recording workspaces at once.
    with ThreadPoolExecutor(max_workers=4) as pool:
        claims = list(pool.map(lambda _: worker.claim(Database(db.settings)), range(4)))
    claims = [c for c in claims if c]
    assert len(claims) == 1 and claims[0]["batch_id"] == batches[0]["id"]
    worker.execute(db, claims[0])
    resumed = Database(db.settings)
    seen_second = False
    for _ in range(80):
        job = worker.claim(resumed)
        if job is None:
            break
        if job["batch_id"] == batches[1]["id"]:
            seen_second = True
            with db.session() as session:
                first = session.get(ProcessingBatch, batches[0]["id"])
                assert first.state == "completed" and first.document["image_cleanup"]
        worker.execute(resumed, job)
    else:
        pytest.fail("Pipelines did not terminate")
    assert seen_second
    with db.session() as session:
        for b in batches:
            row = session.get(ProcessingBatch, b["id"])
            assert row.state == "completed" and row.document["image_cleanup"]
            assert row.document["analysis_result"]["source_id"] in {s["id"] for s in sources}
            assert session.get(Source, row.document["request"]["source_ids"][0]).document["image_retention"]
    # Reports remain readable without the routine image files, including a fresh reconstruction.
    assert source_end_timeline(db, sources[0]["id"])["source_id"] == sources[0]["id"]
    client = TestClient(create_app(db.settings))
    for b in batches:
        assert client.get(f"/api/analyses/{b['id']}/result").status_code == 200


def test_recording_slots_use_all_workers_without_opening_a_second_workspace(db, sources):
    first = start_recording_analysis(db, sources[0]["id"], RecordingAnalysisRequest())
    start_recording_analysis(db, sources[1]["id"], RecordingAnalysisRequest())
    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(pool.map(lambda _: worker.claim(Database(db.settings)), range(8)))
    claims = [c for c in claims if c]
    assert len(claims) == 4
    assert {c["batch_id"] for c in claims} == {first["id"]}


def test_paused_recording_keeps_its_slot_instead_of_filling_cache_with_other_recordings(db, sources):
    from hogline.orchestration.plans import BatchControl, control_batch

    first = start_recording_analysis(db, sources[0]["id"], RecordingAnalysisRequest())
    start_recording_analysis(db, sources[1]["id"], RecordingAnalysisRequest())
    control_batch(db, first["id"], BatchControl(action="pause"))
    assert worker.claim(db) is None
    control_batch(db, first["id"], BatchControl(action="resume"))
    assert worker.claim(db)["batch_id"] == first["id"]


def test_published_skill_assets_survive_recording_cleanup(db, sources):
    from hogline.db import GameRow, SkillMotion, SkillSequence

    sequence_asset = save_image(Image.new("RGB", (40, 40), "red"), db.settings.data_dir)
    motion_asset = save_image(Image.new("RGB", (40, 40), "yellow"), db.settings.data_dir)
    orphan = save_image(Image.new("RGB", (40, 40), "blue"), db.settings.data_dir)
    with db.session() as session:
        session.add(GameRow(id="skill-game", document={}))
        session.flush()
        for model, asset, identifier in [
            (SkillSequence, sequence_asset, "sequence"),
            (SkillMotion, motion_asset, "motion"),
        ]:
            job = Job(state="succeeded", job_key=identifier, document={})
            session.add(job)
            session.flush()
            document = {"asset": asset}
            session.add(
                model(
                    id=identifier,
                    game_id="skill-game",
                    job_id=job.id,
                    document=document,
                    sha256=digest(document),
                )
            )
        session.add(
            ProcessingBatch(
                state="completed",
                document={"storage_policy": POLICY, "request": {"source_ids": [sources[0]["id"]]}},
            )
        )
    with db.session() as session:
        cleanup_completed(session, db.settings.data_dir, review_bytes=0)
    assert read_image_bytes(db.settings.data_dir, sequence_asset)
    assert read_image_bytes(db.settings.data_dir, motion_asset)
    assert not (db.settings.data_dir / orphan["path"]).exists()
