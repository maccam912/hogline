import hashlib

import numpy as np
import pytest
from PIL import Image, ImageDraw
from pydantic import ValidationError
from sqlalchemy import select

from hogline.db import GameRow, SkillMotion
from hogline.orchestration import skill_motion as pipeline
from hogline.orchestration import worker
from hogline.perception.shot_motion import crossing_time, measure_track, motion_detections, motion_tracks
from hogline.perception.tracking import track_handles


def projection():
    return {
        "status": "candidate",
        "pixel_normalized_to_house": np.eye(3).tolist(),
        "origin_px": [240, 240],
        "scale_px": 215,
    }


def rows():
    return [
        {
            "sample_index": i,
            "source_seconds": 10 + i / 10,
            "pts": 100 + i,
            "time_base_num": 1,
            "time_base_den": 10,
            "decoded_rgb_sha256": f"frame-{i}",
            "youtube_url": "",
            "view": "house_top",
            "crop_width": 480,
            "crop_height": 480,
            "requested_interval_seconds": 0.1,
            "detections": [{"center_px": [200 + 35 * i, 240], "color": "red", "boundary_clipped": False}],
        }
        for i in range(4)
    ]


def test_dense_tracking_measures_known_speed_without_inventing_weight_or_aim():
    # Default sparse tracker correctly refuses fast dense input; opt-in gate tracks it.
    assert not track_handles(rows(), "fixture", require_ring=False)["tracks"]
    tracked = motion_tracks(rows(), "fixture")
    assert tracked["minimum_span_seconds"] == 0.2
    assert len(tracked["tracks"]) == 1
    measured = measure_track(tracked["tracks"][0], projection())
    assert measured["nominal_plane_speed_mps"] == pytest.approx(350 / 215 * 1.829)
    assert measured["fit_residual_rms_m"] < 1e-12
    assert measured["weight_multiplier"] is None and measured["aim_error_degrees"] is None
    assert measured["acceptance"] == "proposed"
    assert measured["points"][0]["two_pixel_sensitivity_m"] == pytest.approx(2 / 215 * 1.829)


def test_missing_geometry_or_unsupported_region_abstains_and_gaps_split():
    track = motion_tracks(rows(), "fixture")["tracks"][0]
    assert measure_track(track, {"status": "unavailable"})["reason"] == "house_projection_unavailable"
    track["points"][0]["center_px"] = [900, 900]
    assert measure_track(track, projection())["reason"] == "outside_supported_house_region"
    samples = rows()
    samples[2]["source_seconds"] += 1
    assert not motion_tracks(samples, "gap")["tracks"]


def test_crossing_only_interpolates_observed_short_brackets():
    points = [{"source_seconds": t, "nominal_plane_m": [x, 0]} for t, x in [(1, -1), (1.2, 1), (2, -1)]]
    crossings = crossing_time(points, 0, 0)
    assert len(crossings) == 1
    assert crossings[0]["seconds"] == pytest.approx(1.1)
    assert crossings[0]["bracket_seconds"] == [1, 1.2]


def test_colored_patch_size_prior_retains_stone_sized_patch_and_rejects_small_patch():
    image = Image.new("RGB", (480, 480), "white")
    draw = ImageDraw.Draw(image)
    draw.ellipse((100, 100, 128, 128), fill="red")
    draw.ellipse((300, 300, 310, 310), fill="red")
    result = motion_detections(np.asarray(image), projection())
    assert len(result["detections"]) == len(result["rejected_candidates"]) == 1
    assert result["detections"][0]["center_px"][0] == pytest.approx(114)
    assert result["rejected_candidates"][0]["rejection_reason"] == "outside_local_colored_patch_size_prior"


@pytest.fixture
def motion_game(db, sources, monkeypatch):
    with db.snapshot_session(write=True) as session:
        session.add(
            GameRow(
                id="game",
                document={"spans": [{"source_id": sources[0]["id"], "start_seconds": 0, "end_seconds": 100}]},
            )
        )
    monkeypatch.setattr(pipeline, "profile", lambda: {"views": {"house_top": [0, 0, 1, 1]}})
    monkeypatch.setattr(pipeline, "resolve_media", lambda source: {"uri": "fake:video"})

    def decode(uri, start, end, **kwargs):
        image = Image.new("RGB", (480, 480), (225, 225, 225))
        draw = ImageDraw.Draw(image)
        for radius, color in [
            (215, (30, 45, 190)),
            (143, (225, 225, 225)),
            (72, (30, 160, 90)),
            (24, (225, 225, 225)),
        ]:
            draw.ellipse((240 - radius, 240 - radius, 240 + radius, 240 + radius), fill=color)
        for i in range(min(kwargs["max_frames"], round((end - start) / 0.1))):
            frame = image.copy()
            if start == 10:
                x = 180 + i * 4
                ImageDraw.Draw(frame).ellipse((x - 14, 351, x + 14, 379), fill="red")
            yield frame, {"pts": round(start * 10) + i, "time_base_num": 1, "time_base_den": 10}

    monkeypatch.setattr(pipeline, "decode_window", decode)
    return pipeline.MotionRequest(
        source_id=sources[0]["id"], start_seconds=10, end_seconds=12, reference_seconds=9
    )


def test_worker_retains_original_timestamps_hashes_and_review_frames(db, motion_game):
    from fastapi.testclient import TestClient

    from hogline.review.app import create_app

    job = pipeline.enqueue_motion(db, "game", motion_game)
    assert pipeline.enqueue_motion(db, "game", motion_game)["id"] == job["id"]
    result = worker.run_one(db)
    assert result["state"] == "succeeded", result.get("error")
    with db.snapshot_session() as session:
        document = pipeline.motions_in_session(session, "game")[0]
        assert document["status"] == "experimental_measurements"
        assert not document["stale"]
        assert len(document["samples"]) == 20
        assert len(document["retained_samples"]) == 12
        frame = document["retained_samples"][0]
        image = Image.open(db.settings.data_dir / frame["asset"]["path"])
        sample = document["samples"][frame["sample_index"]]
        assert hashlib.sha256(image.tobytes()).hexdigest() == sample["crop_rgb_sha256"]
        assert sample["pts"] / 10 == sample["source_seconds"]
        assert document["weight_multiplier"] is None
    client = TestClient(create_app(db.settings))
    api = client.get("/api/games/game/skills").json()
    assert api["motions"][0]["id"] == document["id"]
    assert api["motion_jobs"][0]["state"] == "succeeded"
    image_url = f"/api/skill-motion/{document['id']}/images/overlay.png"
    assert client.get(image_url).headers["content-type"] == "image/png"
    assert client.get(f"/api/skill-motion/{document['id']}/images/99999.png").status_code == 422
    with db.snapshot_session(write=True) as session:
        game = session.get(GameRow, "game")
        game.document = {**game.document, "note": "revised"}
    with db.snapshot_session() as session:
        assert pipeline.motions_in_session(session, "game")[0]["stale"]


def test_changed_game_stops_motion_before_decode(db, motion_game, monkeypatch):
    pipeline.enqueue_motion(db, "game", motion_game)
    with db.snapshot_session(write=True) as session:
        game = session.get(GameRow, "game")
        game.document = {**game.document, "note": "revised"}
    monkeypatch.setattr(pipeline, "decode_window", lambda *a, **k: pytest.fail("Must not decode"))
    assert worker.run_one(db)["state"] == "failed"
    with db.snapshot_session() as session:
        assert session.scalar(select(SkillMotion)) is None


def test_image_motion_survives_failed_projection_without_metric_measurements(db, motion_game, monkeypatch):
    monkeypatch.setattr(
        pipeline,
        "diagnose_house_projection",
        lambda rgb: {"status": "review_required", "reasons": ["held_out_geometry_failed"]},
    )
    pipeline.enqueue_motion(db, "game", motion_game)
    result = worker.run_one(db)
    assert result["state"] == "succeeded", result.get("error")
    with db.snapshot_session() as session:
        document = pipeline.motions_in_session(session, "game")[0]
        assert document["status"] == "image_motion_only"
        assert document["image_motion_tracks"] and document["retained_samples"]
        assert all(m["status"] == "unavailable" for m in document["measurements"])
        assert all("nominal_plane_speed_mps" not in m for m in document["measurements"])
        assert document["weight_multiplier"] is None and document["aim_error_degrees"] is None


def test_real_decoder_respects_single_reference_frame_budget(db, motion_game, monkeypatch, video):
    from hogline.media.windows import decode_window

    monkeypatch.setattr(pipeline, "decode_window", decode_window)
    monkeypatch.setattr(pipeline, "resolve_media", lambda source: {"uri": str(video)})
    monkeypatch.setattr(pipeline, "diagnose_house_projection", lambda rgb: {"status": "unavailable"})
    request = pipeline.MotionRequest(
        source_id=motion_game.source_id, start_seconds=1, end_seconds=2, reference_seconds=0.4
    )
    pipeline.enqueue_motion(db, "game", request)
    result = worker.run_one(db)
    assert result["state"] == "succeeded", result.get("error")
    with db.snapshot_session() as session:
        document = pipeline.motions_in_session(session, "game")[0]
        assert document["reference_sample"]["source_seconds"] == pytest.approx(0.4)
        assert document["status"] in ("unavailable", "image_motion_only")
        assert len(document["samples"]) == 10
        assert all(m["status"] == "unavailable" for m in document["measurements"])


def test_motion_bounds_reject_unbounded_decodes_or_reference_outside_game(db, motion_game):
    with pytest.raises(ValidationError):
        pipeline.MotionRequest(source_id="source", start_seconds=10, end_seconds=40, reference_seconds=0)
    with pytest.raises(ValidationError):
        pipeline.MotionRequest(source_id="source", start_seconds=10, end_seconds=12, reference_seconds=11)
    with db.snapshot_session(write=True) as session:
        game = session.get(GameRow, "game")
        game.document = {
            "spans": [{"source_id": motion_game.source_id, "start_seconds": 10, "end_seconds": 100}]
        }
    with pytest.raises(ValueError, match="Reference frame"):
        pipeline.enqueue_motion(db, "game", motion_game)


def test_perspective_tracking_retains_bounded_native_evidence_without_inventing_scale(
    db, sources, monkeypatch
):
    from hogline.adapters.sources import digest
    from hogline.db import Job, SkillSequence
    from hogline.reconstruction.skill_candidates import delivery_candidates
    from tests.test_skill_drafts import sequence

    layout = {"views": {"perspective_right": [0, 0, 1, 1]}}
    monkeypatch.setattr(pipeline, "profile", lambda: layout)
    monkeypatch.setattr(pipeline, "resolve_media", lambda source: {"uri": "fake"})

    def no_projection(_):
        raise AssertionError("Perspective frames must not use a house-plane calibration")

    monkeypatch.setattr(pipeline, "diagnose_house_projection", no_projection)

    def decode(uri, start, end, **kwargs):
        count = min(kwargs["max_frames"], round((end - start) / kwargs["interval"]))
        for i in range(count):
            image = Image.new("RGB", (720, 1026), "white")
            ImageDraw.Draw(image).ellipse((70 + i * 3, 440, 114 + i * 3, 484), fill=(160, 160, 160))
            ImageDraw.Draw(image).ellipse((80 + i * 3, 450, 102 + i * 3, 472), fill="red")
            yield image, {"pts": round(start * 10) + i, "time_base_num": 1, "time_base_den": 10}

    monkeypatch.setattr(pipeline, "decode_window", decode)
    doc = sequence("sequence", release=2)
    doc.update(
        game_id="game",
        game_revision=1,
        source_id=sources[0]["id"],
        job_id="anchor-job",
        source_metadata_sha256=sources[0]["metadata_sha256"],
        profile_sha256=digest(layout),
        request={"view": "perspective_right", "start_seconds": 10, "end_seconds": 12},
    )
    doc["samples"] = [{"source_seconds": 10 + i * 0.5} for i in range(4)]
    with db.snapshot_session(write=True) as session:
        session.add(
            GameRow(
                id="game",
                document={"spans": [{"source_id": sources[0]["id"], "start_seconds": 0, "end_seconds": 100}]},
            )
        )
        session.add(
            Job(id="anchor-job", job_key="anchor-job", state="succeeded", document={"profile": layout})
        )
        session.add(
            SkillSequence(
                id="sequence", game_id="game", job_id="anchor-job", document=doc, sha256=digest(doc)
            )
        )
    candidate = delivery_candidates([doc])[0]
    job = pipeline.enqueue_candidate_motion(db, "game", candidate["id"])
    assert job["request"]["anchor_sequence_id"] == "sequence"
    assert (job["request"]["start_seconds"], job["request"]["end_seconds"]) == (10, 16)
    result = worker.run_one(db)
    assert result["state"] == "succeeded", result.get("error")
    with db.snapshot_session() as session:
        motion = pipeline.motions_in_session(session, "game")[0]
        assert motion["status"] == "image_motion_only"
        assert motion["delivery_anchor"]["release_seconds"] == 11
        assert 1 <= len(motion["retained_samples"]) <= 12
        assert len(motion["samples"]) == 60
        assert motion["image_motion_tracks"]
        assert motion["image_motion_tracks"][0]["relative_release_phase"] == "crosses_proposed_release"
        assert all(m["status"] == "unavailable" for m in motion["measurements"])
        assert motion["weight_multiplier"] is motion["aim_error_degrees"] is None
        image = Image.open(db.settings.data_dir / motion["retained_samples"][0]["asset"]["path"])
        assert image.size == (720, 1026)
    # Full 160-frame perspective clip used to exceed the all-frame memory budget.
    long = pipeline.MotionRequest(
        source_id=sources[0]["id"],
        view="perspective_right",
        start_seconds=10,
        end_seconds=26,
        reference_seconds=10,
    )
    pipeline.enqueue_motion(db, "game", long)
    assert worker.run_one(db)["state"] == "succeeded"
    with db.snapshot_session() as session:
        long_result = next(
            m for m in pipeline.motions_in_session(session, "game") if m["request"]["end_seconds"] == 26
        )
        assert len(long_result["samples"]) == 160 and len(long_result["retained_samples"]) <= 12
    with db.snapshot_session(write=True) as session:
        row = session.get(SkillSequence, "sequence")
        row.document = {**row.document, "changed": True}
        row.sha256 = digest(row.document)
    with db.snapshot_session() as session:
        assert next(m for m in pipeline.motions_in_session(session, "game") if m["delivery_anchor"])["stale"]


def test_proposed_release_phase_is_timing_only():
    from hogline.perception.shot_motion import release_phase

    track = {"start_seconds": 1, "end_seconds": 2}
    assert release_phase(track, None) == "release_unknown"
    assert release_phase(track, 3) == "before_proposed_release"
    assert release_phase(track, 1) == "after_proposed_release"
    assert release_phase(track, 1.5) == "crosses_proposed_release"
    with pytest.raises(ValidationError, match="anchor"):
        pipeline.MotionRequest(
            source_id="s", start_seconds=0, end_seconds=1, reference_seconds=0, anchor_sequence_id="s"
        )


def test_perspective_handles_reject_body_color_and_keep_gray_stone_context():
    from hogline.perception.shot_motion import perspective_detections

    image = Image.new("RGB", (400, 600), (230, 230, 230))
    draw = ImageDraw.Draw(image)
    for x in (100, 250):
        draw.ellipse((x - 15, 290, x + 15, 320), fill=(150, 150, 150))
        draw.ellipse((x - 7, 298, x + 7, 308), fill="yellow")
    people = [{"bbox_px": [225, 250, 50, 100]}]
    result = perspective_detections(np.asarray(image), people)
    assert len(result["detections"]) == 1
    assert result["detections"][0]["center_px"][0] == pytest.approx(100)
    assert result["rejected_candidates"][0]["rejection_reasons"] == ["color_inside_upper_person_box"]


def test_dense_prediction_preserves_a_path_beside_unrelated_crowded_patches():
    samples = []
    for i in range(6):
        r = rows()[0]
        r.update(view="perspective_left", sample_index=i, source_seconds=i / 10, pts=i)
        r["detections"] = [
            dict(center_px=p, color="red", boundary_clipped=False)
            for p in ([200, 200 + 10 * i], [210, 170], [218, 170])
        ]
        samples.append(r)
    old = track_handles(
        samples,
        "old",
        views=("perspective_left",),
        require_ring=False,
        min_span_seconds=0.2,
        max_speed_fraction_per_second=1.2,
    )
    new = motion_tracks(samples, "new")
    assert max((len(t["points"]) for t in old["tracks"]), default=0) < 6
    assert len(new["tracks"]) == 1
    assert len(new["tracks"][0]["points"]) == 6
    assert new["tracks"][0]["points"][-1]["center_px"] == [200, 250]


def test_dense_prediction_does_not_jump_to_a_distant_replacement_patch():
    samples = []
    for i, x in enumerate([100, 105, 140, 145, 150, 155]):
        r = rows()[0]
        r.update(sample_index=i, source_seconds=i / 10, pts=i)
        r["detections"] = [dict(center_px=[x, 240], color="red", boundary_clipped=False)]
        samples.append(r)
    old = track_handles(
        samples, "old", require_ring=False, min_span_seconds=0.2, max_speed_fraction_per_second=1.2
    )
    assert len(old["tracks"][0]["points"]) == 6
    new = motion_tracks(samples, "new")
    assert all(not ({0, 2} <= {p["sample_index"] for p in t["points"]}) for t in new["tracks"])
    assert len(new["tracks"]) == 1 and new["tracks"][0]["start_seconds"] == 0.2


def test_delivery_path_hints_require_known_color_seed_view_and_downward_progress():
    from hogline.perception.shot_motion import delivery_path_proposals

    t = motion_tracks(rows(), "fixture")["tracks"][0]
    t.update(view="perspective_left", crop_width=480)
    t["points"][0]["center_px"] = [200, 200]
    t["points"][-1]["center_px"] = [200, 300]
    t["sampled_path_length_px"] = 100
    proposal = delivery_path_proposals([t], {"stone_color": "red"}, "wausau-composite-seed-v1")[0]
    assert proposal["status"] == "review_first" and proposal["acceptance"] == "proposed"
    assert (
        delivery_path_proposals([t], {"stone_color": "yellow"}, "wausau-composite-seed-v1")[0]["status"]
        == "other_image_fragment"
    )
    assert delivery_path_proposals([t], None, "wausau-composite-seed-v1")[0]["status"] == "unavailable"
    assert (
        delivery_path_proposals([t], {"stone_color": "red"}, "different-layout")[0]["status"] == "unavailable"
    )
    t["points"][-1]["center_px"] = [300, 200]
    assert delivery_path_proposals([t], {"stone_color": "red"}, "wausau-composite-seed-v1")[0]["reasons"] == [
        "little_down_image_progress"
    ]


def test_retained_refinement_reuses_images_without_decode_and_invalidates_on_parent_change(
    db, motion_game, monkeypatch
):
    from fastapi.testclient import TestClient
    from sqlalchemy.orm.attributes import flag_modified

    from hogline.adapters.sources import digest
    from hogline.orchestration.skill_calibration import calibration_draft
    from hogline.orchestration.skill_motion_refinement import refine_motion
    from hogline.review.app import create_app

    pipeline.enqueue_motion(db, "game", motion_game)
    assert worker.run_one(db)["state"] == "succeeded"
    with db.snapshot_session(write=True) as s:
        parent = s.scalar(select(SkillMotion))
        parent_id = parent.id
        parent.document = {**parent.document, "version": "camera-motion-measurements-v4"}
        parent.sha256 = digest(parent.document)
        parent_doc = parent.document

    def forbidden(*args, **kwargs):
        raise AssertionError("Refinement must not decode or fetch media")

    monkeypatch.setattr(pipeline, "decode_window", forbidden)
    monkeypatch.setattr(pipeline, "resolve_media", forbidden)
    first = refine_motion(db, parent_id)
    assert not first["reused"]
    assert refine_motion(db, parent_id)["motion_id"] == first["motion_id"]
    assert refine_motion(db, first["motion_id"])["reused"]
    with db.snapshot_session() as s:
        derived = s.get(SkillMotion, first["motion_id"]).document
        assert derived["samples"] == parent_doc["samples"]
        assert derived["reference_asset"] == parent_doc["reference_asset"]
        assert derived["retained_samples"] == parent_doc["retained_samples"]
        assert len(list(s.scalars(select(SkillMotion)))) == 2
        assert derived["retained_detection_parent"]["sha256"] == digest(parent_doc)
    response = TestClient(create_app(db.settings)).post("/api/skill-motion/" + parent_id + "/refine")
    assert response.status_code == 200 and response.json()["reused"]
    with db.snapshot_session(write=True) as s:
        parent = s.get(SkillMotion, parent_id)
        parent.document["changed"] = True
        parent.sha256 = digest(parent.document)
        flag_modified(parent, "document")
    with db.snapshot_session() as s:
        derived = next(d for d in pipeline.motions_in_session(s, "game") if d["id"] == first["motion_id"])
        assert derived["stale"]
    with pytest.raises(ValueError, match="detection evidence changed"):
        calibration_draft(db, "game", first["motion_id"])
