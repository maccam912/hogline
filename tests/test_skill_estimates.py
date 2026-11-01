import copy

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from pydantic import ValidationError
from sqlalchemy import select

from hogline.adapters.sources import digest
from hogline.db import (
    Evidence,
    GameLineup,
    GameRow,
    LineupLink,
    LineupPerson,
    SkillEstimateRevision,
    Source,
)
from hogline.domain.catalog import Conflict
from hogline.domain.skill_estimates import BroomFeedback, EstimateEdit, estimate_report, save_estimates
from hogline.media.frames import save_image
from hogline.media.retention import protected_evidence
from hogline.review.app import create_app


@pytest.fixture
def example(db, sources):
    source = sources[0]
    sid = source["id"]
    with db.snapshot_session(write=True) as session:
        row = session.get(Source, sid)
        row.document = {
            **row.document,
            "operator_skill_context": {
                "game_id": "game",
                "team": "Koski",
                "stone_color": "yellow",
                "recording_date": "2026-10-30",
            },
        }
        session.add(
            GameRow(
                id="game",
                document={
                    "label": "Test game",
                    "spans": [{"source_id": sid, "start_seconds": 0, "end_seconds": 100}],
                },
            )
        )
        lineup = {
            "source_metadata_sha256": source["metadata_sha256"],
            "start_seconds": 0,
            "end_seconds": 100,
            "players": [{"id": "player"}],
        }
        session.add(
            GameLineup(id="lineup", source_id=sid, batch_id="batch", document=lineup, sha256=digest(lineup))
        )
        session.flush()
        person = {"name": "Lindsey", "team": "Koski"}
        session.add(LineupPerson(id="person", document=person, sha256=digest(person)))
        session.flush()
        link = {"position": "lead", "basis": "operator_confirmed"}
        session.add(
            LineupLink(
                id="player", lineup_id="lineup", person_id="person", document=link, sha256=digest(link)
            )
        )
        for index, seconds in enumerate([9, 10, 12, 14, 35]):
            asset = save_image(Image.new("RGB", (20, 20), (index * 20, 0, 0)), db.settings.data_dir)
            frame = {
                "source_metadata_sha256": source["metadata_sha256"],
                "source_seconds": seconds,
                "image": asset,
                "crops": {},
            }
            session.add(
                Evidence(id=f"frame-{seconds}", source_id=sid, evidence_key=f"key-{seconds}", document=frame)
            )
    return EstimateEdit(
        expected_revision=0,
        reason="Independent footage review",
        deliveries=[
            {
                "candidate_id": "reviewed-delivery:test",
                "person_id": "person",
                "source_id": sid,
                "observed_start_seconds": 10,
                "observed_end_seconds": 14,
                "basis": "Original video and named lineup",
                "evidence_ids": ["frame-9", "frame-10", "frame-12"],
                "summary": "Stone entered the rings; intended call unknown",
                "finish": "inside_rings",
            }
        ],
        findings=[
            {
                "person_id": "person",
                "text": "One observed house stone",
                "candidate_ids": ["reviewed-delivery:test"],
            }
        ],
    )


def test_report_uses_named_lineup_and_color_without_inventing_accuracy(db, example):
    save_estimates(db, "game", example)
    report = estimate_report(db, "game")
    p = report["players"][0]
    assert p["name"] == "Lindsey" and p["position"] == "lead"
    assert p["observed_deliveries"] == 1
    assert p["weight_accuracy"] is None and p["release_aim_accuracy"] is None
    assert p["deliveries"][0]["acceptance"] == "assistant_estimate"
    assert report["coverage"]["attributed_deliveries"] == 1
    assert not report["stale"]
    with db.snapshot_session() as session:
        assert {"frame-9", "frame-10", "frame-12"} <= protected_evidence(session)


def test_revisions_are_append_only_and_conflict_checked(db, example):
    first = save_estimates(db, "game", example)
    with pytest.raises(Conflict):
        save_estimates(db, "game", example)
    example.expected_revision = 1
    example.deliveries[0].summary = "Corrected interpretation"
    save_estimates(db, "game", example)
    with db.snapshot_session() as session:
        rows = list(session.scalars(select(SkillEstimateRevision).order_by(SkillEstimateRevision.number)))
        assert len(rows) == 2 and rows[0].sha256 == first["sha256"]
        assert rows[0].document["review"]["deliveries"][0]["summary"] != example.deliveries[0].summary


@pytest.mark.parametrize(
    "change,reason",
    [
        ("context", "team_context_changed"),
        ("name", "named_lineup_changed"),
        ("frame", "review_frame_changed"),
        ("image", "review_image_unavailable"),
        ("source", "source_changed"),
    ],
)
def test_changed_inputs_withhold_old_estimates(db, example, change, reason):
    save_estimates(db, "game", example)
    with db.snapshot_session(write=True) as session:
        if change in ("context", "source"):
            row = session.get(Source, example.deliveries[0].source_id)
            doc = copy.deepcopy(row.document)
            if change == "source":
                doc["metadata_sha256"] = "changed"
            else:
                doc["operator_skill_context"]["stone_color"] = "red"
            row.document = doc
        elif change == "name":
            row = session.get(LineupPerson, "person")
            row.document = {**row.document, "name": "Corrected name"}
            row.sha256 = digest(row.document)
        else:
            row = session.get(Evidence, "frame-10")
            if change == "frame":
                row.document = {**row.document, "source_seconds": 11}
            else:
                (db.settings.data_dir / row.document["image"]["path"]).unlink()
    report = estimate_report(db, "game")
    assert report["stale"] and reason in report["stale_reasons"]
    assert all(not p["deliveries"] and not p["findings"] for p in report["players"])


def test_requires_two_action_frames_and_prevents_double_counting(db, example):
    example.deliveries[0].evidence_ids = ["frame-9", "frame-10"]
    with pytest.raises(ValueError, match="at least two"):
        save_estimates(db, "game", example)
    example.deliveries[0].evidence_ids.append("frame-12")
    other = example.deliveries[0].model_copy(update={"candidate_id": "reviewed-delivery:duplicate"})
    example.deliveries.append(other)
    with pytest.raises(ValueError, match="overlap"):
        save_estimates(db, "game", example)


def test_foreign_identity_and_evidence_rejected(db, example, sources):
    example.deliveries[0].person_id = "unlinked"
    with pytest.raises(ValueError, match="named player"):
        save_estimates(db, "game", example)
    example.deliveries[0].person_id = "person"
    with db.snapshot_session(write=True) as session:
        row = session.get(Evidence, "frame-12")
        row.source_id = sources[1]["id"]
    with pytest.raises(ValueError, match="inside this game|source changed"):
        save_estimates(db, "game", example)


def test_feedback_keeps_post_delivery_indication_separate_from_call(db, example):
    d = example.deliveries[0]
    d.evidence_ids.append("frame-35")
    d.feedback = BroomFeedback.model_validate(
        {
            "called_evidence_id": "frame-9",
            "indicated_evidence_id": "frame-35",
            "view": "house_top",
            "called_x": 0.4,
            "indicated_x": 0.6,
            "ring_left_x": 0.1,
            "ring_right_x": 0.9,
            "basis": "Possible feedback gesture, interpretation uncertain",
        }
    )
    save_estimates(db, "game", example)
    p = estimate_report(db, "game")["players"][0]
    feedback = p["deliveries"][0]["feedback_comparison"]
    assert feedback["interpretation"] == "possible_feedback"
    assert feedback["image_direction"] == "right"
    assert feedback["nominal_lateral_shift_m"] == pytest.approx(0.91)
    assert p["release_aim_accuracy"] is None
    example.expected_revision = 1
    example.deliveries[0].feedback.called_evidence_id = "frame-35"
    with pytest.raises(ValueError, match="original call before"):
        save_estimates(db, "game", example)


def test_api_and_html_escape_user_content(db, example):
    example.findings[0].text = '<script>alert("x")</script>'
    client = TestClient(create_app(db.settings))
    response = client.put("/api/games/game/skill-estimates", json=example.model_dump())
    assert response.status_code == 200
    report = client.get("/api/games/game/skill-estimates/report")
    assert report.status_code == 200
    assert '<script>alert("x")</script>' not in report.text
    assert "&lt;script&gt;" in report.text and "Lindsey" in report.text
    assert "/static/skills.html?game=game" in report.text
    assert client.get("/api/games/game/skill-estimates").json()["revision"] == 1
    assert client.get("/api/games/game/skill-estimates/history").json()["revisions"][0]["revision"] == 1


def test_duplicate_ids_and_unsupported_findings_rejected(db, example):
    with pytest.raises(ValidationError):
        EstimateEdit.model_validate(
            {
                **example.model_dump(),
                "deliveries": [example.deliveries[0].model_dump(), example.deliveries[0].model_dump()],
            }
        )
    example.findings[0].candidate_ids = ["unreviewed-shot"]
    with pytest.raises(ValueError, match="this player's"):
        save_estimates(db, "game", example)


def test_frame_acquisition_is_local_bounded_and_keeps_original_timestamps(db, example, monkeypatch):
    from hogline.orchestration import skill_estimate_frames as frames

    sid = example.deliveries[0].source_id
    request = frames.EstimateFramesRequest(source_id=sid, seconds=[10, 12, 10])
    with pytest.raises(ValueError, match="verified local"):
        frames.capture_estimate_frames(db, "game", request)
    with db.snapshot_session(write=True) as session:
        source = session.get(Source, sid)
        source.document = {**source.document, "platform": "local"}
    monkeypatch.setattr(frames, "resolve_media", lambda source: {"uri": "local-fixture"})
    monkeypatch.setattr(
        frames,
        "capture",
        lambda uri, seconds: (
            Image.new("RGB", (100, 100)),
            {"pts": int(seconds * 100), "time_base_num": 1, "time_base_den": 100},
        ),
    )
    result = frames.capture_estimate_frames(db, "game", request)
    assert result["paid_requests"] == 0
    assert [f["seconds"] for f in result["frames"]] == [10, 12]
    assert result == frames.capture_estimate_frames(db, "game", request)
    request.seconds = [101]
    with pytest.raises(ValueError, match="inside this game"):
        frames.capture_estimate_frames(db, "game", request)


def test_automatic_candidate_color_conflict_cannot_attribute_opponent(db, example, monkeypatch):
    from hogline.domain import skill_estimates

    monkeypatch.setattr(
        skill_estimates,
        "delivery_candidates",
        lambda sequences: [
            {
                "id": "auto-red",
                "source_id": example.deliveries[0].source_id,
                "start_seconds": 10,
                "end_seconds": 14,
                "stone_color": "red",
                "ambiguous_overlap": False,
            }
        ],
    )
    example.deliveries[0].candidate_id = "auto-red"
    with pytest.raises(ValueError, match="stone color conflicts"):
        save_estimates(db, "game", example)


def test_front_of_house_placement_is_not_scored_as_a_failed_draw(db, example):
    example.deliveries[0].finish = "in_front_of_rings"
    example.deliveries[0].summary = "Yellow rests in front of the house; call unknown"
    save_estimates(db, "game", example)
    p = estimate_report(db, "game")["players"][0]
    assert p["finish_observations"] == {"in_front_of_rings": 1}
    assert p["weight_accuracy"] is None and p["release_aim_accuracy"] is None
    assert "success_percentage" not in p


def test_delivery_rays_pin_evidence_and_withhold_changed_frames(db, example):
    from hogline.db import SkillDeliveryRay

    save_estimates(db, "game", example)
    report = estimate_report(db, "game")
    with db.snapshot_session(write=True) as session:
        frame = session.get(Evidence, "frame-9")
        source = session.get(Source, frame.source_id)
        doc = {
            "review_sha256": report["review_sha256"],
            "source_id": source.id,
            "source_metadata_sha256": source.document["metadata_sha256"],
            "frame_pins": {"frame-9": digest(frame.document)},
        }
        session.add(
            SkillDeliveryRay(
                id="ray",
                game_id="game",
                candidate_id="reviewed-delivery:test",
                plan_key="plan",
                document=doc,
                sha256=digest(doc),
            )
        )
    report = estimate_report(db, "game")
    assert report["players"][0]["deliveries"][0]["delivery_ray"]["id"] == "ray"
    with db.snapshot_session(write=True) as session:
        assert "frame-9" in protected_evidence(session)
        frame = session.get(Evidence, "frame-9")
        frame.document = {**frame.document, "changed": True}
    assert estimate_report(db, "game")["players"][0]["deliveries"] == []


def test_delivery_ray_withholds_corrupt_measurements_without_erasing_report(db, example):
    from hogline.db import SkillDeliveryRay

    save_estimates(db, "game", example)
    report = estimate_report(db, "game")
    with db.snapshot_session(write=True) as session:
        frame = session.get(Evidence, "frame-9")
        source = session.get(Source, frame.source_id)
        doc = {
            "review_sha256": report["review_sha256"],
            "source_id": source.id,
            "source_metadata_sha256": source.document["metadata_sha256"],
            "frame_pins": {},
        }
        session.add(
            SkillDeliveryRay(
                id="old-valid-ray",
                game_id="game",
                candidate_id="reviewed-delivery:test",
                plan_key="old-plan",
                document=doc,
                sha256=digest(doc),
                created_at="2000-01-01",
            )
        )
        session.add(
            SkillDeliveryRay(
                id="ray",
                game_id="game",
                candidate_id="reviewed-delivery:test",
                plan_key="plan",
                document=doc,
                sha256="corrupt",
                created_at="2001-01-01",
            )
        )
    shot = estimate_report(db, "game")["players"][0]["deliveries"][0]
    assert shot["delivery_ray"] is None


def test_perspective_extension_is_immutable_deduplicates_and_pins_paired_frames(db, example):
    from hogline.db import SkillDeliveryRay
    from hogline.orchestration.perspective_rays import publish_perspectives

    save_estimates(db, "game", example)
    report = estimate_report(db, "game")
    with db.snapshot_session(write=True) as session:
        frame = session.get(Evidence, "frame-9")
        source = session.get(Source, frame.source_id)
        base = {
            "candidate_id": "reviewed-delivery:test",
            "review_sha256": report["review_sha256"],
            "source_id": source.id,
            "source_metadata_sha256": source.document["metadata_sha256"],
            "media_sha256": "verified-video",
            "frame_pins": {"frame-9": digest(frame.document)},
            "origin_frame_id": "frame-9",
            "house_split": {"status": "partial_house_only"},
        }
        session.add(
            SkillDeliveryRay(
                id="base-ray",
                game_id="game",
                candidate_id=base["candidate_id"],
                plan_key="native-plan",
                document=base,
                sha256=digest(base),
                created_at="2000-01-01",
            )
        )
        repeated = {**frame.document, "evidence_key": "key-9"}
        new = {**frame.document, "evidence_key": "new-pair", "source_seconds": 15}
    cal = {
        "source_id": base["source_id"],
        "source_metadata_sha256": base["source_metadata_sha256"],
        "media_sha256": "verified-video",
        "reference_frame_id": "pending-reference",
        "pairs": [{"frame_id": "new-pair"}],
        "cameras": {},
    }
    frames = {"pending-reference": repeated, "new-pair": new}
    published = publish_perspectives(db, report, {"id": "base-ray", **base}, cal, frames)
    assert not published["cached"]
    assert published["document"]["perspective_calibration"]["reference_frame_id"] == "frame-9"
    assert set(published["document"]["frame_pins"]) == {"frame-9", "new-pair"}
    assert published["document"]["delivery_split_proxy"]["elapsed_seconds"] is None
    assert publish_perspectives(db, report, {"id": "base-ray", **base}, cal, frames)["cached"]
    current = estimate_report(db, "game")
    assert current["review_sha256"] == report["review_sha256"]
    assert current["players"][0]["deliveries"][0]["delivery_ray"]["id"] == published["id"]
    with db.snapshot_session(write=True) as session:
        assert session.get(SkillDeliveryRay, "base-ray").document == base
        assert {"new-pair", "frame-9"} <= protected_evidence(session)
        pair = session.get(Evidence, "new-pair")
        pair.document = {**pair.document, "changed": True}
    # A corrupt paired frame suppresses the newest extension, not the player
    # report, and never silently falls back to the older overhead-only ray.
    shot = estimate_report(db, "game")["players"][0]["deliveries"][0]
    assert shot["delivery_ray"] is None
