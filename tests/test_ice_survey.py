import hashlib
import json
from uuid import NAMESPACE_URL, uuid5

import pytest
from PIL import Image
from sqlalchemy import select

from hogline.adapters.sources import digest
from hogline.analytics.dense import SCHEMA_VERSION as OLD_SCHEMA
from hogline.config import profile
from hogline.db import Evidence, Job, ScoringSegment
from hogline.media.frames import observe
from hogline.orchestration.ice import SCHEMA_VERSION, create_ice_batch, crop
from hogline.orchestration.worker import run_one


def references(db, source):
    layout = profile()
    image = Image.new("RGB", (640, 360), "white")
    timing = {"pts": 0, "time_base_num": 1, "time_base_den": 10}
    frame, _ = observe(image, timing, source, db.settings.data_dir, layout=layout)
    frame["id"] = str(uuid5(NAMESPACE_URL, frame["evidence_key"]))
    with db.session() as s:
        s.add(
            Evidence(
                id=frame["id"], evidence_key=frame["evidence_key"], source_id=source["id"], document=frame
            )
        )
    refs = []
    for view in ["house_top", "house_bottom"]:
        bounds = layout["views"][view]
        house = crop(image, bounds)
        ref = {
            "source_id": source["id"],
            "evidence_id": frame["id"],
            "image": frame["image"],
            "source_metadata_sha256": source["metadata_sha256"],
            "evidence_document_sha256": digest(frame),
            "view": view,
            "bounds": bounds,
            "crop_size": list(house.size),
            "crop_rgb_sha256": hashlib.sha256(house.tobytes()).hexdigest(),
            "geometry_profile_sha256": digest(layout),
            "status": "visually_reviewed_empty_scoring_house",
        }
        ref["sha256"] = digest(ref)
        refs.append(ref)
    path = db.settings.data_dir / "references.json"
    path.write_text(json.dumps({"references": refs}))
    return path, image, timing


def test_ice_survey_is_idempotent_bounded_and_pins_custom_artifact(db, sources, monkeypatch):
    import pyarrow.parquet as pq

    from hogline.orchestration import ice

    path, image, timing = references(db, sources[0])
    monkeypatch.setattr(ice, "resolve_media", lambda source: {"uri": "unused", "format_id": "fixture"})

    def decoder(*args, **kwargs):
        for n in range(4):
            yield image, {**timing, "pts": n * 50}

    monkeypatch.setattr(ice, "decode_scoring_segment", decoder)
    batch = create_ice_batch(db, sources[0]["id"], references_path=path, end=20)
    again = create_ice_batch(db, sources[0]["id"], references_path=path, end=20)
    assert again["id"] == batch["id"]
    run_one(db)
    with db.session() as s:
        jobs = list(s.scalars(select(Job)))
        assert [j.state for j in jobs] == ["succeeded"]
        segment = s.scalar(select(ScoringSegment)).document
    assert segment["sample_count"] == 4
    assert segment["coverage_kind"] == "source_grid_keyframe_survey"
    assert len(segment["anchor_evidence_ids"]) == 2
    assert segment["parquet"]["schema"] == SCHEMA_VERSION != OLD_SCHEMA
    artifact = db.settings.data_dir / segment["parquet"]["path"]
    assert hashlib.sha256(artifact.read_bytes()).hexdigest() == segment["parquet"]["sha256"]
    rows = pq.read_table(artifact).to_pylist()
    assert len(rows) == 8
    assert [r["pts"] for r in rows[::2]] == [0, 50, 100, 150]
    assert all(json.loads(r["measurements_json"])["status"] == "unobservable" for r in rows)
    assert create_ice_batch(db, sources[0]["id"], references_path=path, end=20)["state"] == "completed"


def test_ice_survey_rejects_changed_reference_pixels_before_video_access(db, sources):
    path, _, _ = references(db, sources[0])
    manifest = json.loads(path.read_text())
    manifest["references"][0]["crop_rgb_sha256"] = "0" * 64
    ref = manifest["references"][0]
    ref["sha256"] = digest({k: v for k, v in ref.items() if k != "sha256"})
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="pixels changed"):
        create_ice_batch(db, sources[0]["id"], references_path=path, end=20)
    with db.session() as s:
        assert s.scalar(select(Job)) is None


def test_ice_survey_failure_publishes_no_partial_segment_or_evidence(db, sources, monkeypatch):
    from hogline.media.errors import TransientMediaError
    from hogline.orchestration import ice

    path, image, timing = references(db, sources[0])
    monkeypatch.setattr(ice, "resolve_media", lambda source: {"uri": "unused", "format_id": "fixture"})

    def broken(*args, **kwargs):
        yield image, timing
        raise TransientMediaError("network interrupted")

    monkeypatch.setattr(ice, "decode_scoring_segment", broken)
    create_ice_batch(db, sources[0]["id"], references_path=path, end=20)
    run_one(db)
    with db.session() as s:
        job = s.scalar(select(Job))
        assert job.state == "queued" and job.document["retry_pending"]
        assert s.scalar(select(ScoringSegment)) is None
        assert len(list(s.scalars(select(Evidence)))) == 1


def test_scoring_artifact_rejects_corruption_and_timeline_does_not_complete_game(db, sources, monkeypatch):
    from hogline.analytics.ice import source_ice_timeline
    from hogline.orchestration import ice

    path, image, timing = references(db, sources[0])
    monkeypatch.setattr(ice, "resolve_media", lambda source: {"uri": "unused", "format_id": "fixture"})

    def decoder(*args, **kwargs):
        for n in range(4):
            yield image, {**timing, "pts": n * 50}

    monkeypatch.setattr(ice, "decode_scoring_segment", decoder)
    create_ice_batch(db, sources[0]["id"], references_path=path, end=20)
    run_one(db)
    report = source_ice_timeline(db, sources[0]["id"])
    assert report["sample_count"] == 4
    assert "game_completion" not in report
    assert report["views"]["house_top"]["unobservable_samples"] == 4
    with db.session() as s:
        segment = s.scalar(select(ScoringSegment)).document
    file = db.settings.data_dir / segment["parquet"]["path"]
    file.write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="missing or corrupt"):
        source_ice_timeline(db, sources[0]["id"])
