import numpy as np
import pytest
from PIL import Image
from sqlalchemy.orm.attributes import flag_modified

from hogline.adapters.sources import digest
from hogline.db import GameRow, Job, SkillMotion
from hogline.domain.skill_calibration import CameraCalibration
from hogline.domain.skills import SkillDocument, SkillEdit, read_skills, save_skills
from hogline.media.frames import save_image
from hogline.orchestration.skill_calibration import (
    calibration_draft,
    calibration_preview,
    calibration_reports,
)
from hogline.perception.landmark_calibration import fit_calibration, map_points, project_track


def calibration(**changes):
    # Unequal scale, perspective, skew and translation: not an identity-only test.
    h = np.array([[0.015, 0.003, -1], [-0.002, 0.024, 1], [0.0004, 0.0007, 1]])
    pixels = [[30, 40], [470, 50], [450, 450], [50, 430], [240, 80], [270, 390]]
    world = map_points(pixels, h)
    return CameraCalibration.model_validate(
        {
            "id": "cal1",
            "motion_id": "motion",
            "motion_sha256": "a" * 64,
            "source_id": "source",
            "view": "perspective_right",
            "image_sha256": "b" * 64,
            "start_seconds": 9,
            "end_seconds": 13,
            "coordinate_system": "X right, Y along sheet from surveyed origin",
            "basis": "Synthetic surveyed fixture",
            "evidence": ["fixture-dimensions"],
            "acceptance": "confirmed",
            "landmarks": [
                dict(label=f"p{i}", pixel=p, plane_m=q.tolist(), use="fit" if i < 4 else "validation")
                for i, (p, q) in enumerate(zip(pixels, world, strict=True))
            ],
            **changes,
        }
    )


def track():
    # Use a linear metric trajectory, then render it through the inverse camera.
    fit = fit_calibration(calibration(), (500, 500))
    origin = map_points([[220, 220]], fit["matrix"])[0]
    world = [origin + np.array([0.3, 1.7]) * i / 10 for i in range(6)]
    pixels = map_points(world, np.linalg.inv(fit["matrix"]))
    return dict(
        id="path",
        view="perspective_right",
        start_seconds=10,
        end_seconds=10.5,
        points=[dict(source_seconds=10 + i / 10, center_px=p.tolist()) for i, p in enumerate(pixels)],
    )


def test_independent_landmarks_recover_perspective_and_known_motion():
    c = calibration()
    fit = fit_calibration(c, (500, 500))
    assert fit["status"] == "validated_geometry"
    assert max(fit["fit_errors_m"] + fit["validation_errors_m"]) < 1e-10
    projected = project_track(track(), c, fit)
    assert projected["speed_mps"] == pytest.approx(np.hypot(0.3, 1.7))
    assert projected["velocity_plane_mps"] == pytest.approx([0.3, 1.7])
    assert projected["fit_residual_rms_m"] < 1e-10
    assert projected["weight_multiplier"] is projected["aim_error_degrees"] is None
    assert projected["acceptance"] == "proposed"
    assert all(p["two_pixel_sensitivity_m"] > 0 for p in projected["points"])


def test_holdout_error_blocks_even_a_perfect_four_point_fit():
    c = calibration()
    c.landmarks[-1].plane_m[1] += 0.2
    fit = fit_calibration(c, (500, 500))
    assert max(fit["fit_errors_m"]) < 1e-10
    assert fit["status"] == "review_required"
    assert "independent_validation_exceeds_tolerance" in fit["reasons"]
    assert project_track(track(), c, fit)["speed_mps"] is None


@pytest.mark.parametrize(
    "mutation,reason",
    [
        ("few", "four_fit_and_two_independent_validation_landmarks_required"),
        ("reuse", "validation_reuses_fit_location"),
        ("duplicate", "duplicate_landmark_locations"),
        ("outside", "landmark_outside_reference_image"),
        ("collinear", "insufficient_landmark_spread"),
        ("world_collinear", "degenerate_or_unstable_homography"),
    ],
)
def test_bad_landmark_geometry_is_unavailable(mutation, reason):
    c = calibration()
    if mutation == "few":
        c.landmarks.pop()
    elif mutation == "reuse":
        c.landmarks[-1].pixel = c.landmarks[0].pixel.copy()
    elif mutation == "duplicate":
        c.landmarks[-1].pixel = c.landmarks[-2].pixel.copy()
    elif mutation == "outside":
        c.landmarks[-1].pixel = [500, 250]
    elif mutation == "collinear":
        for i, p in enumerate(c.landmarks[:4]):
            p.pixel = [50 + i * 100, 200]
    else:
        for i, p in enumerate(c.landmarks[:4]):
            p.plane_m = [i, 0]
    fit = fit_calibration(c, (500, 500))
    assert fit["status"] == "unavailable"
    assert reason in fit["reasons"]


def test_no_extrapolation_or_unconfirmed_speed_and_timing_gaps_rejected():
    c = calibration()
    fit = fit_calibration(c, (500, 500))
    t = track()
    t["points"][0]["center_px"] = [490, 490]
    assert project_track(t, c, fit)["reason"] == "track_outside_supported_landmark_region"
    t = track()
    t["points"][1]["source_seconds"] += 0.4
    assert project_track(t, c, fit)["reason"] == "track_timing_gap_or_reversal"
    t = track()
    t["view"] = "perspective_left"
    assert project_track(t, c, fit)["reason"] == "track_outside_calibration_scope"
    c.acceptance = "proposed"
    assert project_track(track(), c, fit)["speed_mps"] is None


@pytest.fixture
def camera_game(db, sources):
    sid = sources[0]["id"]
    asset = save_image(Image.new("RGB", (500, 500), "white"), db.settings.data_dir)
    sample = dict(source_seconds=9, crop_width=500, crop_height=500)
    document = dict(
        id="motion",
        game_id="game",
        game_revision=1,
        source_id=sid,
        source_metadata_sha256=sources[0]["metadata_sha256"],
        request=dict(view="perspective_right", start_seconds=10, end_seconds=13),
        reference_sample=sample,
        reference_asset=asset,
        retained_samples=[],
        samples=[],
        image_motion_tracks=[track()],
    )
    with db.snapshot_session(write=True) as session:
        session.add(
            GameRow(id="game", document={"spans": [dict(source_id=sid, start_seconds=0, end_seconds=100)]})
        )
        session.add(Job(id="job", job_key="calibration-fixture", state="succeeded", document={}))
        session.flush()
        session.add(
            SkillMotion(
                id="motion",
                game_id="game",
                job_id="job",
                document=document,
                sha256=digest(document),
            )
        )
    return calibration(source_id=sid, motion_sha256=digest(document), image_sha256=asset["sha256"])


def test_calibration_persistence_and_changed_motion_invalidates_reports(db, camera_game):
    c = camera_game
    draft = calibration_draft(db, "game", "motion")
    assert draft["image_size"] == [500, 500]
    assert draft["document"]["landmarks"] == []
    preview = calibration_preview(db, "game", c)
    assert preview["measurements"][0]["speed_mps"] == pytest.approx(np.hypot(0.3, 1.7))
    saved = save_skills(db, "game", SkillEdit(expected_revision=0, document=SkillDocument(calibrations=[c])))
    assert not saved["stale"]
    with db.snapshot_session(write=True) as session:
        row = session.get(SkillMotion, "motion")
        row.document["changed"] = True
        row.sha256 = digest(row.document)
        flag_modified(row, "document")
    with db.snapshot_session() as session:
        assert read_skills(session, "game")["stale"]
    result = calibration_reports(db, "game", [c])[0]
    assert result["status"] == "unavailable" and not result["measurements"]
    with pytest.raises(ValueError, match="different motion"):
        save_skills(db, "game", SkillEdit(expected_revision=1, document=SkillDocument(calibrations=[c])))


@pytest.mark.parametrize(
    "change,error",
    [
        ({"view": "perspective_left"}, "different motion"),
        ({"image_sha256": "0" * 64}, "reference image changed"),
        ({"reference_sample_index": 0}, "original retained frame"),
        ({"start_seconds": 10}, "inside its validity interval"),
        ({"end_seconds": 101}, "game source span"),
    ],
)
def test_calibration_is_bound_to_owned_image_camera_and_time(db, camera_game, change, error):
    c = camera_game.model_copy(update=change)
    with pytest.raises(ValueError, match=error):
        calibration_preview(db, "game", c)


def test_invalid_confirmed_fit_cannot_be_saved_but_proposal_can(db, camera_game):
    c = camera_game
    c.landmarks[-1].plane_m[0] += 1
    edit = SkillEdit(expected_revision=0, document=SkillDocument(calibrations=[c]))
    with pytest.raises(ValueError, match="independent landmark validation"):
        save_skills(db, "game", edit)
    c.acceptance = "proposed"
    assert save_skills(db, "game", edit)["revision"] == 1


def test_preview_endpoint_and_no_implicit_player_statistics(db, camera_game):
    from fastapi.testclient import TestClient

    from hogline.review.app import create_app

    client = TestClient(create_app(db.settings))
    c = camera_game
    response = client.post("/api/games/game/skills/calibration-preview", json=c.model_dump())
    assert response.status_code == 200, response.text
    assert response.json()["fit"]["status"] == "validated_geometry"
    save_skills(db, "game", SkillEdit(expected_revision=0, document=SkillDocument(calibrations=[c])))
    # Calibration output is separate from skill_report; no shot is created by a camera fit.
    from hogline.analytics.skills import skill_report

    with db.snapshot_session() as session:
        doc = SkillDocument.model_validate(read_skills(session, "game")["document"])
    assert not skill_report(doc)["groups"]
    assert calibration_reports(db, "game", doc.calibrations)[0]["measurements"]


def aim_observation(c, **changes):
    from hogline.domain.skill_aim import AimObservation

    return AimObservation.model_validate(
        {
            "calibration_id": c.id,
            "calibration_sha256": digest(c.model_dump()),
            "track_id": "path",
            "release_seconds": 9.9,
            "start_seconds": 10,
            "end_seconds": 10.5,
            "call_seconds": 9,
            "line_origin_m": [0, 0],
            "broom_m": [0, 30],
            "right_side_m": [1, 0],
            "reviewed_free_travel": True,
            "reviewed_stone_association": True,
            "reviewed_called_line": True,
            "acceptance": "confirmed",
            "basis": "Known synthetic called line",
            "evidence": ["fixture-call"],
            **changes,
        }
    )


def test_short_post_release_path_produces_separate_line_and_turn_aware_error():
    from hogline.perception.shot_aim import measure_aim

    c = calibration()
    fit = fit_calibration(c, (500, 500))
    aim = aim_observation(c)
    result = measure_aim(aim, c, fit, track(), "left")
    assert result["line_error_degrees"] == pytest.approx(np.degrees(np.arctan(0.3 / 1.7)))
    assert result["wide_error_degrees"] == result["line_error_degrees"]
    assert result["heading_change_degrees"] == pytest.approx(0, abs=1e-10)
    assert result["weight_multiplier"] is None
    right = measure_aim(aim, c, fit, track(), "right")
    assert right["wide_error_degrees"] == -right["line_error_degrees"]
    assert measure_aim(aim, c, fit, track())["wide_error_degrees"] is None
    c.basis = "Changed calibration review"
    assert measure_aim(aim, c, fit, track())["reason"] == "camera_calibration_changed"


def test_aim_gates_review_and_supported_observation_window():
    from hogline.perception.shot_aim import measure_aim

    c = calibration()
    fit = fit_calibration(c, (500, 500))
    aim = aim_observation(c, acceptance="proposed", reviewed_free_travel=False)
    assert measure_aim(aim, c, fit, track())["line_error_degrees"] is None
    aim = aim_observation(c, end_seconds=10.8)
    assert measure_aim(aim, c, fit, track())["reason"] == "aim_window_endpoint_not_observed"
    aim = aim_observation(c)
    t = track()
    t["points"][2]["source_seconds"] += 0.4
    assert measure_aim(aim, c, fit, t)["reason"] == "track_timing_gap_or_reversal"


def test_saved_aim_recomputes_from_evidence_without_changing_weight_reference(db, camera_game):
    from hogline.analytics.skills import skill_report
    from hogline.domain.skills import Shot
    from hogline.orchestration.skill_calibration import aim_preview

    c = camera_game
    shot = Shot(
        id="shot",
        source_id=c.source_id,
        seconds=9.9,
        side="red",
        end=1,
        participant="Red lead",
        call="draw",
        turn="left",
        direction="near_to_far",
        acceptance="confirmed",
        evidence=["fixture"],
        speed_mps=1.8,
        target_speed_mps=2,
        calibration="split-plane",
        line_error_degrees=77,
        aim=aim_observation(c),
    )
    document = SkillDocument(calibrations=[c], shots=[shot])
    preview = aim_preview(db, "game", document, shot)
    assert preview["measurement"]["status"] == "experimental_line_measurement"
    saved = save_skills(db, "game", SkillEdit(expected_revision=0, document=document))
    stored = SkillDocument.model_validate(saved["document"])
    assert stored.shots[0].line_error_degrees == pytest.approx(np.degrees(np.arctan(0.3 / 1.7)))
    assert stored.shots[0].calibration == "split-plane"
    assert stored.shots[0].line_calibration == c.id
    summary = skill_report(stored)["summaries"][0]
    assert summary["weight"]["mean"] == 0.9
    assert summary["line"]["n"] == summary["wide"]["n"] == 1
    assert summary["line"]["sample_sd"] is None
    # Editing an unrelated shot field retains a verified measurement; changing calibration invalidates it.
    stored.calibrations[0].basis = "Changed survey"
    with pytest.raises(ValueError, match="calibration_changed"):
        save_skills(db, "game", SkillEdit(expected_revision=1, document=stored))


def test_proposed_aim_cannot_enter_reports_even_with_confirmed_shot(db, camera_game):
    from hogline.analytics.skills import skill_report
    from hogline.domain.skills import Shot

    c = camera_game
    s = Shot(
        id="shot",
        source_id=c.source_id,
        seconds=9.9,
        end=1,
        side="red",
        participant="Red lead",
        call="draw",
        direction="near_to_far",
        acceptance="confirmed",
        evidence=["fixture"],
        aim=aim_observation(c, acceptance="proposed"),
    )
    saved = save_skills(
        db, "game", SkillEdit(expected_revision=0, document=SkillDocument(calibrations=[c], shots=[s]))
    )
    doc = SkillDocument.model_validate(saved["document"])
    assert doc.shots[0].line_error_degrees is None
    assert skill_report(doc)["summaries"][0]["line"]["n"] == 0


def test_parallel_path_keeps_lateral_offset_separate_from_angle():
    from hogline.perception.shot_aim import measure_aim

    c = calibration()
    fit = fit_calibration(c, (500, 500))
    t = track()
    start = map_points([t["points"][0]["center_px"]], fit["matrix"])[0]
    velocity = np.array([0.3, 1.7])
    right = np.array([1.7, -0.3]) / np.linalg.norm(velocity)
    origin = start - 2 * right
    aim = aim_observation(
        c,
        line_origin_m=origin.tolist(),
        broom_m=(origin + velocity * 10).tolist(),
        right_side_m=(origin + right).tolist(),
    )
    result = measure_aim(aim, c, fit, t)
    assert result["line_error_degrees"] == pytest.approx(0, abs=1e-10)
    assert result["start_right_offset_m"] == pytest.approx(2)
    assert result["end_right_offset_m"] == pytest.approx(2)


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"source_id": "other-source"}, "source and delivery time"),
        ({"seconds": 50}, "source and delivery time"),
        ({"side": "yellow"}, "color conflicts"),
    ],
)
def test_aim_cannot_attach_to_another_delivery_or_opposite_color(changes, reason):
    from hogline.domain.skills import Shot
    from hogline.orchestration.skill_calibration import checked_shot_aim

    c = calibration()
    t = {**track(), "color": "red"}
    s = Shot.model_validate(
        {
            "id": "shot",
            "source_id": c.source_id,
            "seconds": 9.9,
            "side": "red",
            "aim": aim_observation(c),
            **changes,
        }
    )
    checked = {c.id: (c, {"motion": {"image_motion_tracks": [t]}, "fit": fit_calibration(c, (500, 500))})}
    with pytest.raises(ValueError, match=reason):
        checked_shot_aim(s, checked)


@pytest.fixture
def transferred_camera_game(db, camera_game):
    import cv2

    rng = np.random.default_rng(423)
    original = Image.fromarray(rng.integers(0, 255, (500, 500, 3), dtype=np.uint8))
    matrix = np.array([[1.0, 0, 3], [0, 1, -2], [0, 0, 1]])
    target = Image.fromarray(cv2.warpPerspective(np.asarray(original), matrix, original.size))
    original_asset, target_asset = [save_image(image, db.settings.data_dir) for image in (original, target)]
    with db.snapshot_session(write=True) as session:
        row = session.get(SkillMotion, "motion")
        row.document = {**row.document, "reference_asset": original_asset}
        row.sha256 = digest(row.document)
        c = camera_game.model_copy(
            update={"image_sha256": original_asset["sha256"], "motion_sha256": row.sha256}
        )
        target_track = track()
        target_track["start_seconds"], target_track["end_seconds"] = 60, 60.5
        for point in target_track["points"]:
            point["source_seconds"] += 50
            point["center_px"] = map_points([point["center_px"]], matrix)[0].tolist()
        doc = {
            **row.document,
            "id": "target",
            "request": dict(view="perspective_right", start_seconds=60, end_seconds=63),
            "reference_sample": dict(source_seconds=59, crop_width=500, crop_height=500),
            "reference_asset": target_asset,
            "image_motion_tracks": [target_track],
        }
        session.add(Job(id="target-job", job_key="target-fixture", state="succeeded", document={}))
        session.flush()
        session.add(
            SkillMotion(id="target", game_id="game", job_id="target-job", document=doc, sha256=digest(doc))
        )
    save_skills(db, "game", SkillEdit(expected_revision=0, document=SkillDocument(calibrations=[c])))
    return c


def test_original_calibration_serialization_preserves_pre_transfer_checksums():
    c = calibration()
    assert "transfer" not in c.model_dump()
    original = c.model_dump()
    assert digest(CameraCalibration.model_validate(original).model_dump()) == digest(original)


def test_camera_transfer_requires_independent_review_and_preserves_coordinate_cohort(
    db, transferred_camera_game
):
    from hogline.orchestration.skill_calibration import calibration_cohort
    from hogline.orchestration.skill_camera_transfer import CalibrationTransferRequest, transfer_calibration

    parent = transferred_camera_game
    result = transfer_calibration(
        db,
        "game",
        CalibrationTransferRequest(
            parent_id=parent.id, parent_sha256=digest(parent.model_dump()), target_motion_id="target"
        ),
    )
    assert result["registration"]["status"] == "alignment_proposal"
    c = CameraCalibration.model_validate(result["document"])
    assert c.acceptance == "proposed" and c.transfer.target_landmarks_reviewed is False
    assert result["fit"]["status"] == "review_required"
    assert "transferred_landmarks_need_independent_target_frame_review" in result["fit"]["reasons"]
    assert all(m["speed_mps"] is None for m in result["measurements"])
    assert calibration_cohort(c) == parent.id
    c.acceptance = "confirmed"
    with pytest.raises(ValueError, match="independent landmark validation"):
        save_skills(
            db, "game", SkillEdit(expected_revision=1, document=SkillDocument(calibrations=[parent, c]))
        )
    c.transfer.target_landmarks_reviewed = True
    save_skills(db, "game", SkillEdit(expected_revision=1, document=SkillDocument(calibrations=[parent, c])))
    report = calibration_reports(db, "game", [parent, c])[1]
    assert report["fit"]["status"] == "validated_geometry"
    assert report["measurements"][0]["speed_mps"] == pytest.approx(np.hypot(0.3, 1.7), abs=0.02)
    assert report["cohort_id"] == parent.id
    with pytest.raises(ValueError, match="parent is absent"):
        save_skills(db, "game", SkillEdit(expected_revision=2, document=SkillDocument(calibrations=[c])))
    changed = parent.model_copy(update={"basis": "A changed physical map review"})
    with pytest.raises(ValueError, match="parent is absent, changed"):
        save_skills(
            db, "game", SkillEdit(expected_revision=2, document=SkillDocument(calibrations=[changed, c]))
        )
    c.landmarks[-1].plane_m[0] += 0.01
    with pytest.raises(ValueError, match="original landmark dimensions"):
        save_skills(
            db, "game", SkillEdit(expected_revision=2, document=SkillDocument(calibrations=[parent, c]))
        )


def test_camera_comparison_rejects_other_views_and_api_can_propose_without_accepting(
    db, transferred_camera_game
):
    from fastapi.testclient import TestClient

    from hogline.review.app import create_app

    client = TestClient(create_app(db.settings))
    response = client.post(
        "/api/games/game/skill-motion/target/camera-comparison", json={"reference_motion_id": "motion"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "alignment_proposal"
    parent = transferred_camera_game
    response = client.post(
        "/api/games/game/skills/calibration-transfer",
        json={
            "parent_id": parent.id,
            "parent_sha256": digest(parent.model_dump()),
            "target_motion_id": "target",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["document"]["acceptance"] == "proposed"
    with db.snapshot_session(write=True) as session:
        row = session.get(SkillMotion, "target")
        row.document = {**row.document, "request": {**row.document["request"], "view": "perspective_left"}}
        row.sha256 = digest(row.document)
    response = client.post(
        "/api/games/game/skill-motion/target/camera-comparison", json={"reference_motion_id": "motion"}
    )
    assert response.status_code == 422 and "same recording and camera" in response.text


def test_transferred_aim_joins_original_coordinate_cohort_without_losing_own_evidence(
    db, transferred_camera_game
):
    from hogline.analytics.skills import skill_report
    from hogline.domain.skills import Shot
    from hogline.orchestration.skill_camera_transfer import CalibrationTransferRequest, transfer_calibration

    parent = transferred_camera_game
    proposal = transfer_calibration(
        db,
        "game",
        CalibrationTransferRequest(
            parent_id=parent.id, parent_sha256=digest(parent.model_dump()), target_motion_id="target"
        ),
    )
    child = CameraCalibration.model_validate(proposal["document"])
    child.transfer.target_landmarks_reviewed = True
    child.acceptance = "confirmed"
    shots = []
    for c, offset in [(parent, 0), (child, 50)]:
        aim = aim_observation(c)
        for field in ["release_seconds", "start_seconds", "end_seconds", "call_seconds"]:
            setattr(aim, field, getattr(aim, field) + offset)
        shots.append(
            Shot(
                id=f"shot-{offset}",
                source_id=c.source_id,
                seconds=9.9 + offset,
                end=1,
                side="red",
                participant="Red lead",
                call="draw",
                turn="left",
                direction="near_to_far",
                acceptance="confirmed",
                evidence=["fixture"],
                aim=aim,
            )
        )
    saved = save_skills(
        db,
        "game",
        SkillEdit(expected_revision=1, document=SkillDocument(calibrations=[parent, child], shots=shots)),
    )
    document = SkillDocument.model_validate(saved["document"])
    assert document.shots[1].aim.calibration_id == child.id
    assert document.shots[1].aim.calibration_sha256 == digest(child.model_dump())
    assert all(s.line_calibration == parent.id for s in document.shots)
    summaries = skill_report(document)["summaries"]
    assert len(summaries) == 1 and summaries[0]["line"]["n"] == 2
    assert summaries[0]["line"]["sample_sd"] < 0.1
