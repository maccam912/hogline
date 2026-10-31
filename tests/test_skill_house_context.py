import copy
import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from hogline.adapters.sources import digest
from hogline.config import profile
from hogline.db import GameRow, Job, SkillSequence
from hogline.media.frames import save_image
from hogline.orchestration.skill_house_context import (
    analyze_house_context,
    build_context,
    crop_image,
    crop_reference,
    latest_house_context,
)
from hogline.perception.shot_sequence import VERSION, contact_sheets
from hogline.reconstruction.skill_house_context import infer_house_context
from hogline.review.app import create_app
from tests.test_house_states import row


def context_rows():
    return [
        row(
            i,
            (80, 100, 120) if i < 4 else (),
            source_seconds=i * 2,
            requested_interval_seconds=2,
            image_reference={"frame_index": i},
        )
        for i in range(9)
    ]


def test_clearing_is_proposed_and_retains_before_after_evidence():
    result = infer_house_context(context_rows(), "key")
    (clearing,) = result["possible_clearings"]
    assert clearing["acceptance"] == "proposed" and clearing["official_end_number"] is None
    assert (clearing["earliest_seconds"], clearing["latest_seconds"]) == (6, 8)
    assert clearing["before"]["image_reference"] == {"frame_index": 3}
    assert clearing["after"]["image_reference"] == {"frame_index": 4}
    assert all(s["score"] is None for s in result["states"])
    assert "Outside stones are omitted" in result["notice"]
    assert result == infer_house_context(list(reversed(context_rows())), "key")


@pytest.mark.parametrize("problem", ["gap", "short_zero", "camera", "foreground", "repeat", "outer_only"])
def test_missing_or_ambiguous_evidence_never_becomes_clearing(problem):
    rows = context_rows()
    if problem == "gap":
        for r in rows[4:]:
            r["source_seconds"] += 20
    if problem == "short_zero":
        rows = rows[:6]
    if problem == "camera":
        for r in rows[4:]:
            r["ring_reference"]["center_px"][0] += 10
    if problem == "foreground":
        for r in rows[4:]:
            r["foreground_regions"] = [{"bbox_px": [90, 90, 20, 20]}]
    if problem == "repeat":
        for r in rows[4:]:
            r["decoded_rgb_sha256"] = "same"
    if problem == "outer_only":
        for r in rows[:4]:
            for d in r["detections"]:
                d["center_px"][0] = 10
    assert infer_house_context(rows, "key")["possible_clearings"] == []


def survey(db, sources):
    frames = []
    for i in range(8):
        image = Image.new("RGB", (1920, 1080), "white")
        draw = ImageDraw.Draw(image)
        draw.ellipse((730, 10, 1190, 470), outline="blue", width=65)
        draw.ellipse((730, 610, 1190, 1070), outline="blue", width=65)
        draw.rectangle((i * 5, 0, i * 5 + 3, 10), fill="red")
        frames.append((i * 2, image))
    assets = [save_image(image, db.settings.data_dir) for _, image in contact_sheets(frames)]
    layout = profile()
    samples = [
        {
            "index": i,
            "source_seconds": i * 2,
            "pts": i * 2,
            "time_base_num": 1,
            "time_base_den": 1,
            "width": 1920,
            "height": 1080,
            "decoded_rgb_sha256": digest(i),
            "normalized_crop_bounds": [0, 0, 1, 1],
            "youtube_url": "https://www.youtube.com/watch?v=AAAAAAAAAAA",
        }
        for i in range(8)
    ]
    doc = {
        "id": "seq",
        "game_id": "game",
        "job_id": "job",
        "game_revision": 1,
        "source_id": sources[0]["id"],
        "source_metadata_sha256": sources[0]["metadata_sha256"],
        "profile_sha256": digest(layout),
        "version": VERSION,
        "samples": samples,
        "sheet_assets": assets,
        "request": {"view": "composite", "start_seconds": 0, "end_seconds": 16, "interval_seconds": 2},
    }
    with db.snapshot_session(write=True) as session:
        session.add(
            GameRow(
                id="game",
                document={
                    "id": "game",
                    "spans": [{"source_id": sources[0]["id"], "start_seconds": 0, "end_seconds": 16}],
                },
            )
        )
        session.add(Job(id="job", job_key="job", state="succeeded", document={"profile": layout}))
        session.add(SkillSequence(id="seq", game_id="game", job_id="job", document=doc, sha256=digest(doc)))
    return {"id": "seq", "sha256": digest(doc), "document": doc, "profile": layout}


def test_retained_crop_layout_and_cache_and_http(db, sources):
    sequence = survey(db, sources)
    for i in (0, 1, 2, 3, 4, 7):
        ref = crop_reference(sequence, i, "house_top")
        image = crop_image(db.settings.data_dir, sequence, ref)
        assert image.size == (128, 128)
        assert image.getpixel((64, 64)) == (255, 255, 255)
        assert ref["sheet_index"] == i // 4
    result = analyze_house_context(db, "game")
    assert result["sample_count"] == 8 and not result["cached"]
    assert analyze_house_context(db, "game")["cached"]
    with db.snapshot_session() as session:
        assert not latest_house_context(session, "game")["stale"]
    client = TestClient(create_app(db.settings))
    assert client.post("/api/games/game/skills/house-context").json()["cached"]
    assert result["groups"][0]["states"]
    url = f"/api/skill-house-context/{result['id']}/0/0/first.png"
    response = client.get(url)
    assert response.status_code == 200
    assert Image.open(io.BytesIO(response.content)).size == (128, 128)
    assert client.get(url.replace("/0/0/", "/0/999/")).status_code == 422
    with db.snapshot_session(write=True) as session:
        row = session.get(SkillSequence, "seq")
        row.document = {**row.document, "changed": True}
        row.sha256 = digest(row.document)
    with db.snapshot_session() as session:
        assert latest_house_context(session, "game")["stale"]
    assert client.get(url).status_code == 422


def test_unsupported_dimensions_and_corrupt_asset_are_reported(db, sources):
    sequence = survey(db, sources)
    bad = copy.deepcopy(sequence)
    bad["document"]["samples"][0]["width"] = 1000
    with pytest.raises(ValueError, match="dimensions"):
        crop_reference(bad, 0, "house_top")
    assert len(build_context(db.settings.data_dir, [bad], "key")["skipped_frames"]) == 1
    path = db.settings.data_dir / sequence["document"]["sheet_assets"][0]["path"]
    path.write_bytes(b"broken")
    with pytest.raises(ValueError, match="checksum"):
        build_context(db.settings.data_dir, [sequence], "key")


def test_conflicting_same_time_images_exclude_all_versions(db, sources):
    sequence = survey(db, sources)
    second = copy.deepcopy(sequence)
    second["document"]["samples"][0]["decoded_rgb_sha256"] = "different"
    result = build_context(db.settings.data_dir, [sequence, second], "key")
    assert result["sample_count"] == 7
    assert len(result["skipped_frames"]) == 2
    assert all(s["reason"] == "conflicting_decoded_images_at_source_time" for s in result["skipped_frames"])
    assert result["groups"][0]["coverage_intervals"][0]["start_seconds"] == 2


def test_geometry_change_is_retained_as_unresolved_not_silently_lost():
    rows = context_rows()
    for r in rows[4:]:
        r["ring_reference"]["center_px"][0] += 10
    result = infer_house_context(rows, "key")
    assert not result["possible_clearings"]
    (change,) = result["unresolved_changes"]
    assert change["kind"] == "unresolved_house_change"
    assert change["unresolved_reasons"] == ["ring_geometry_changed"]
    assert change["official_end_number"] is None


def test_bounded_local_work_never_publishes_truncated_result(db, sources, monkeypatch):
    import hogline.orchestration.skill_house_context as context

    survey(db, sources)
    monkeypatch.setattr(context, "MAX_FRAMES", 3)
    with pytest.raises(ValueError, match="frame/time bound"):
        analyze_house_context(db, "game")
    with db.snapshot_session() as session:
        assert latest_house_context(session, "game") is None
