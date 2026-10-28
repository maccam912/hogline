import hashlib
import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw
from sqlalchemy import func, select

from hogline.analytics.exports import snapshot
from hogline.db import Evidence, FrameObservation
from hogline.media.frames import observe
from hogline.perception.board_locator import VERSION, analyze_board_location, locate_board
from hogline.review.app import create_app


def board_image(size=(1920, 1080), *, second=False, header=True, obstructed=False):
    image = Image.new("RGB", (1920, 1080), (210, 210, 210))
    draw = ImageDraw.Draw(image)
    for x in [110, 410] if second else [110]:
        draw.rectangle((x, 100, x + 249, 124), fill=(180, 25, 45))
        draw.rectangle((x, 125, x + 249, 149), fill=(30, 40, 100) if header else "white")
        draw.rectangle((x, 150, x + 249, 174), fill=(220, 200, 30))
        # Movable cards must not split a whole board into different locations.
        draw.rectangle((x + 40, 101, x + 51, 124), fill="white")
        if obstructed:
            draw.rectangle((x + 80, 144, x + 170, 180), fill=(20, 20, 20))
    return image.resize(size)


@pytest.mark.parametrize("size", [(1920, 1080), (1280, 720)])
def test_board_geometry_scales_and_does_not_infer_columns_or_scores(size):
    result = locate_board(board_image(size), [0, 0, 0.375, 0.95])
    assert result["status"] == "proposed" and result["candidate_count"] == 1
    bounds = result["selected"]["normalized_bounds"]
    assert bounds == pytest.approx([110 / 1920, 100 / 1080, 360 / 1920, 175 / 1080], abs=0.002)
    assert result["point_columns"] is None and result["blank_end_convention"] is None
    assert result["selected"]["acceptance"] == "proposed"


def test_missing_header_obstruction_and_multiple_boards_do_not_force_a_location():
    panel = [0, 0, 0.375, 0.95]
    assert locate_board(board_image(header=False), panel)["status"] == "unavailable"
    assert locate_board(board_image(obstructed=True), panel)["status"] == "unavailable"
    result = locate_board(board_image(second=True), panel)
    assert result["status"] == "ambiguous" and result["selected"] is None
    assert len(result["candidates"]) == 2


def test_locations_are_immutable_crop_hashes_link_to_original_and_export(db, sources):
    doc, _ = observe(
        board_image(),
        {"pts": 1500, "time_base_num": 1, "time_base_den": 10},
        sources[0],
        db.settings.data_dir,
    )
    with db.session() as session:
        frame = Evidence(source_id=sources[0]["id"], evidence_key=doc["evidence_key"], document=doc)
        session.add(frame)
        session.flush()
        location = analyze_board_location(session, frame, db.settings.data_dir)
        session.flush()
        assert analyze_board_location(session, frame, db.settings.data_dir) == location
        assert session.scalar(select(func.count()).select_from(FrameObservation)) == 1
        assert location["model_version"] == VERSION and location["score"] is None
        assert location["youtube_url"].endswith("&t=150s")
    with TestClient(create_app(db.settings)) as client:
        url = f"/api/board-locations/{location['id']}/0.png"
        response = client.get(url)
        assert response.status_code == 200
        crop = Image.open(io.BytesIO(response.content)).convert("RGB")
        assert hashlib.sha256(crop.tobytes()).hexdigest() == location["crops"][0]["rgb_sha256"]
        assert client.get(f"/api/board-locations/{location['id']}/99.png").status_code == 404
        data = client.get("/api/catalog").json()
        assert data["board_locations"][0]["id"] == location["id"]
        release = snapshot(db, "exploratory")
        assert release["board_locations"][0]["source_image_sha256"] == doc["image"]["sha256"]
        (db.settings.data_dir / doc["image"]["path"]).write_bytes(b"corrupt")
        assert client.get(url).status_code == 410


def test_coarse_frames_are_not_board_evidence(db, sources):
    doc, _ = observe(
        board_image((640, 360)),
        {"pts": 0, "time_base_num": 1, "time_base_den": 1},
        sources[0],
        db.settings.data_dir,
    )
    doc["acquisition_tier"] = "coarse"
    with db.session() as session:
        frame = Evidence(source_id=sources[0]["id"], evidence_key=doc["evidence_key"], document=doc)
        session.add(frame)
        session.flush()
        assert analyze_board_location(session, frame, db.settings.data_dir) is None
        assert session.scalar(select(func.count()).select_from(FrameObservation)) == 0
