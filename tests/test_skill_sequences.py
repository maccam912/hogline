from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest
from PIL import Image
from pydantic import ValidationError
from sqlalchemy import select

from hogline.db import GameRow, Job, SkillSequence
from hogline.orchestration import skills, worker
from hogline.perception.shot_sequence import DeliveryProposal, contact_sheets
from hogline.perception.vision_config import VisionConfig


@pytest.fixture
def skill_game(db, sources, monkeypatch):
    with db.snapshot_session(write=True) as session:
        session.add(
            GameRow(
                id="game",
                document={"spans": [{"source_id": sources[0]["id"], "start_seconds": 0, "end_seconds": 100}]},
            )
        )
    monkeypatch.setattr(
        skills, "load_vision_config", lambda: VisionConfig("secret", "gpt-6.1-terra", Decimal(5), True)
    )
    monkeypatch.setattr(skills, "resolve_media", lambda source: {"uri": "fake:video"})

    def decode(*args, **kwargs):
        for n in range(4):
            yield (
                Image.new("RGB", (1920, 1080), (n * 30, 20, 30)),
                {"pts": 10 + n, "time_base_num": 1, "time_base_den": 1},
            )

    monkeypatch.setattr(skills, "decode_window", decode)

    def vision(root, sheets, **kwargs):
        assert len(sheets) == 1
        assert kwargs["context"]["source_id"] == sources[0]["id"]
        return {
            "id": "vision-result",
            "proposal": {
                "call": "unknown",
                "call_basis": "Intent uncertain",
                "broom_visible": False,
                "delivery_visible": True,
                "sweeping_visible": False,
                "observations": [],
                "limitations": [],
                "activities": [
                    {
                        "actor_tag": "A1",
                        "description": "Gray shirt",
                        "first_frame": 0,
                        "last_frame": 3,
                        "key_frame": 1,
                        "bbox": [0.1, 0.1, 0.3, 0.7],
                        "action": "delivery",
                        "team_side": "red",
                        "side_basis": "Controls the red stone during delivery",
                        "stone_color": "red",
                        "delivery_index": 0,
                        "evidence_basis": "Slide across frames",
                        "limitations": [],
                    }
                ],
                "deliveries": [
                    {
                        "first_frame": 0,
                        "last_frame": 3,
                        "release_frame": 2,
                        "stone_color": "red",
                        "thrower_description": "Gray shirt",
                        "thrower_actor_tag": "A1",
                        "call": "unknown",
                        "call_basis": "Not visible",
                        "broom_frame": None,
                        "broom_tip": None,
                        "limitations": [],
                    }
                ],
            },
        }

    monkeypatch.setattr(skills, "propose_call", vision)
    return skills.SequenceRequest(
        source_id=sources[0]["id"], start_seconds=10, end_seconds=14, interval_seconds=1
    )


def test_worker_publishes_source_bound_proposals_and_reuses_job(db, skill_game):
    from fastapi.testclient import TestClient

    from hogline.review.app import create_app

    job = skills.enqueue_sequence(db, "game", skill_game)
    assert skills.enqueue_sequence(db, "game", skill_game)["id"] == job["id"]
    result = worker.run_one(db)
    assert result["state"] == "succeeded", result.get("error")
    with db.snapshot_session() as session:
        sequences = skills.sequences_in_session(session, "game")
        assert len(sequences) == 1
        sequence = sequences[0]
        assert sequence["acceptance"] == "proposed"
        assert [s["source_seconds"] for s in sequence["samples"]] == [10, 11, 12, 13]
        assert len(sequence["sheet_assets"]) == 1
        assert sequence["proposal"]["deliveries"][0]["release_frame"] == 2
        assert sequence["activity_assets"][0]["asset"]["width"] == 384
    assert skills.enqueue_sequence(db, "game", skill_game)["state"] == "succeeded"
    client = TestClient(create_app(db.settings))
    data = client.get("/api/games/game/skills").json()
    actor = data["actor_proposals"][0]
    candidate = data["delivery_candidates"][0]
    assert candidate["review"]["draft"]["participant"] == actor["id"]
    assert data["candidate_coverage"]["unreviewed"] == 1
    saved = client.put(
        "/api/games/game/skills",
        json={
            "expected_revision": 0,
            "document": {"slots": [], "shots": [candidate["review"]["draft"]]},
        },
    )
    assert saved.status_code == 200, saved.text
    reviewed = client.get("/api/games/game/skills").json()
    assert reviewed["candidate_coverage"]["with_saved_review"] == 1
    assert reviewed["report"]["excluded"] == {"proposed": 1}
    assert actor["role_proposals"] == ["thrower"]
    assert actor["throwing_position"] is None and actor["skip"] is None
    assert client.get(f"/api/skill-sequences/{sequence['id']}/actors/0.png").status_code == 200


def test_parallel_claims_serialize_paid_vision_without_blocking_local_jobs(db, skill_game):
    from hogline.orchestration.plans import enqueue_in_session

    skills.enqueue_sequence(db, "game", skill_game)
    skills.enqueue_sequence(
        db, "game", skill_game.model_copy(update={"start_seconds": 20, "end_seconds": 24})
    )
    with db.snapshot_session(write=True) as session:
        enqueue_in_session(session, {"kind": "local_test_task"})
    with ThreadPoolExecutor(max_workers=4) as pool:
        claims = list(pool.map(lambda _: worker.claim(db), range(4)))
    assert sum(c is not None and c["kind"] == "skill_sequence" for c in claims) == 1
    assert sum(c is not None and c["kind"] == "local_test_task" for c in claims) == 1
    with db.snapshot_session() as session:
        assert len(list(session.scalars(select(Job).where(Job.state == "queued")))) == 1


def test_participant_memberships_are_revisioned_and_bound_to_current_game_evidence(db, skill_game):
    from hogline.adapters.sources import digest
    from hogline.domain.skills import (
        Participant,
        SkillDocument,
        SkillEdit,
        read_skills,
        save_skills,
        skill_history,
    )

    skills.enqueue_sequence(db, "game", skill_game)
    assert worker.run_one(db)["state"] == "succeeded"
    with db.snapshot_session() as session:
        sequence = skills.sequences_in_session(session, "game")[0]
    participant = Participant(
        id="skill-player:p1",
        label="Anonymous player 1",
        actor_ids=[f"skill-actor:{sequence['id']}:A1"],
        acceptance="confirmed",
        basis="Reviewed this clip-local participant",
    )
    edit = SkillEdit(expected_revision=0, document=SkillDocument(participants=[participant]))
    assert save_skills(db, "game", edit)["revision"] == 1
    with db.snapshot_session() as session:
        assert skill_history(session, "game")[0]["document"]["participants"][0]["id"] == participant.id
        assert not read_skills(session, "game")["stale"]
    with db.snapshot_session(write=True) as session:
        session.add(
            GameRow(
                id="other",
                document={
                    "spans": [{"source_id": skill_game.source_id, "start_seconds": 0, "end_seconds": 100}]
                },
            )
        )
    with pytest.raises(ValueError, match="game and source"):
        save_skills(db, "other", edit)
    edit.expected_revision = 1
    edit.document.participants[0].actor_ids = [f"skill-actor:{sequence['id']}:A99"]
    with pytest.raises(ValueError, match="absent"):
        save_skills(db, "game", edit)
    with db.snapshot_session(write=True) as session:
        row = session.get(SkillSequence, sequence["id"])
        row.document = {**row.document, "changed_evidence": True}
        row.sha256 = digest(row.document)
    with db.snapshot_session() as session:
        assert read_skills(session, "game")["stale"]


def test_queued_sequences_and_actor_evidence_survive_only_audited_scope_extension(db, skill_game):
    from hogline.db import Revision
    from hogline.domain.models import Game
    from hogline.domain.skill_scope import CoverageExtension, extend_skill_coverage
    from hogline.domain.skills import Participant, SkillDocument, SkillEdit, save_skills

    with db.snapshot_session(write=True) as session:
        game = session.get(GameRow, "game")
        game.document = Game(
            id="game", sheet=5, coverage="partial", spans=game.document["spans"]
        ).model_dump()
        session.flush()
        revision = game.revision
        session.add(
            Revision(
                game_id="game", number=revision, document=game.document, author="test", reason="Initial scope"
            )
        )
    skills.enqueue_sequence(db, "game", skill_game)
    extend_skill_coverage(
        db,
        "game",
        CoverageExtension(expected_game_revision=revision, span_index=0, start_seconds=0, end_seconds=120),
    )
    assert worker.run_one(db)["state"] == "succeeded"
    with db.snapshot_session() as session:
        sequence = skills.sequences_in_session(session, "game")[0]
        assert not sequence["stale"] and sequence["game_revision"] == revision
    participant = Participant(
        id="skill-player:p1",
        label="Anonymous 1",
        actor_ids=[f"skill-actor:{sequence['id']}:A1"],
        basis="Reviewed same observation",
    )
    assert (
        save_skills(
            db, "game", SkillEdit(expected_revision=0, document=SkillDocument(participants=[participant]))
        )["revision"]
        == 1
    )


def test_pre_delivery_call_evidence_is_anchored_retained_and_kept_out_of_ratings(db, skill_game, monkeypatch):
    from fastapi.testclient import TestClient

    from hogline.perception.call_targets import VERSION as CALL_VERSION
    from hogline.review.app import create_app

    skills.enqueue_sequence(db, "game", skill_game)
    assert worker.run_one(db)["state"] == "succeeded"
    client = TestClient(create_app(db.settings))
    candidate = client.get("/api/games/game/skills").json()["delivery_candidates"][0]
    path = f"/api/games/game/skill-candidates/{candidate['id']}/call-evidence"
    response = client.post(path)
    assert response.status_code == 200, response.text
    jobs = response.json()["jobs"]
    assert len(jobs) == 2 and all(j["request"]["end_seconds"] == 10 for j in jobs)
    assert client.post(path).json()["jobs"] == jobs

    def decode(uri, start, end, interval, **kwargs):
        for n in range(int((end - start) / interval)):
            yield (
                Image.new("RGB", (1920, 1080), "white"),
                {
                    "pts": int((start + n * interval) * 1000),
                    "time_base_num": 1,
                    "time_base_den": 1000,
                },
            )

    def vision(root, sheets, **kwargs):
        assert kwargs["context"]["version"] == CALL_VERSION
        assert kwargs["context"]["call_anchor"]["sequence_id"]
        return {
            "id": "call-test",
            "proposal": {
                "call": "unknown",
                "call_basis": "Broom does not establish shot type",
                "broom_visible": True,
                "delivery_visible": False,
                "sweeping_visible": False,
                "observations": [],
                "limitations": [],
                "deliveries": [],
                "activities": [],
                "targets": [
                    {
                        "frame": 1,
                        "kind": "called_line",
                        "point": [0.5, 0.5],
                        "actor_tag": None,
                        "evidence_basis": "Planted broom head",
                        "limitations": ["Receiving end unknown"],
                    }
                ],
                "strategy_options": [
                    {"call": "draw", "rationale": "Open house", "missing_context": ["Hammer"]}
                ],
            },
        }

    monkeypatch.setattr(skills, "decode_window", decode)
    monkeypatch.setattr(skills, "propose_call", vision)
    assert worker.run_one(db)["state"] == "succeeded"
    assert worker.run_one(db)["state"] == "succeeded"
    data = client.get("/api/games/game/skills").json()
    assert len(data["call_evidence"]) == 2
    assert data["document"]["shots"] == []
    for call in data["call_evidence"]:
        assert call["candidate_ids"] == [candidate["id"]]
        assert call["call"] == "unknown" and call["strategy_options"][0]["call"] == "draw"
        assert call["targets"][0]["source_seconds"] == 1
        assert call["targets"][0]["coordinate_status"] == "uncalibrated_image_point"
        for kind in ("native", "overlay"):
            assert (
                client.get(f"/api/skill-sequences/{call['sequence_id']}/targets/0/{kind}.png").status_code
                == 200
            )
    bad = {**jobs[0]["request"], "start_seconds": 9, "end_seconds": 12}
    rejected = client.post("/api/games/game/skill-sequences", json=bad)
    assert rejected.status_code == 422 and "precede" in rejected.text
    bad = {**jobs[0]["request"], "anchor_sha256": "changed"}
    assert client.post("/api/games/game/skill-sequences", json=bad).status_code == 422


def test_actor_ids_are_game_source_and_revision_bound(db, skill_game):
    from hogline.domain.skills import Shot, SkillDocument, SkillEdit, save_skills
    from hogline.reconstruction.skill_actors import actor_proposals

    skills.enqueue_sequence(db, "game", skill_game)
    assert worker.run_one(db)["state"] == "succeeded"
    with db.snapshot_session() as session:
        actor = actor_proposals(skills.sequences_in_session(session, "game"))[0]
    shot = Shot(
        id="shot",
        source_id=skill_game.source_id,
        seconds=11,
        participant=actor["id"],
        evidence=actor["evidence"],
    )
    save_skills(db, "game", SkillEdit(expected_revision=0, document=SkillDocument(shots=[shot])))
    shot.participant = actor["id"].replace(":A1", ":A99")
    with pytest.raises(ValueError, match="absent"):
        save_skills(db, "game", SkillEdit(expected_revision=1, document=SkillDocument(shots=[shot])))
    shot.participant = actor["id"]
    shot.seconds = 30
    with pytest.raises(ValueError, match="scoped to this sequence"):
        save_skills(db, "game", SkillEdit(expected_revision=1, document=SkillDocument(shots=[shot])))
    shot.seconds = 11
    with db.snapshot_session(write=True) as session:
        session.add(
            GameRow(
                id="different-game",
                document={
                    "spans": [{"source_id": skill_game.source_id, "start_seconds": 0, "end_seconds": 100}]
                },
            )
        )
    with pytest.raises(ValueError, match="no longer matches"):
        save_skills(
            db, "different-game", SkillEdit(expected_revision=0, document=SkillDocument(shots=[shot]))
        )


def test_changed_game_stops_job_before_paid_request(db, skill_game, monkeypatch):
    job = skills.enqueue_sequence(db, "game", skill_game)
    with db.snapshot_session(write=True) as session:
        row = session.get(GameRow, "game")
        row.document = {**row.document, "note": "changed"}
    monkeypatch.setattr(skills, "propose_call", lambda *a, **k: pytest.fail("Must not call API"))
    assert worker.run_one(db)["state"] == "failed"
    with db.snapshot_session() as session:
        assert session.scalar(select(SkillSequence)) is None
        assert "changed" in session.get(Job, job["id"]).document["error"]


def test_overlapping_camera_observations_cannot_be_counted_as_two_confirmed_shots(db, skill_game):
    from hogline.domain.skills import Shot, SkillDocument, SkillEdit, save_skills

    skills.enqueue_sequence(db, "game", skill_game)
    assert worker.run_one(db)["state"] == "succeeded"
    second = skill_game.model_copy(update={"view": "perspective_left"})
    skills.enqueue_sequence(db, "game", second)
    assert worker.run_one(db)["state"] == "succeeded"
    with db.snapshot_session() as session:
        sequences = skills.sequences_in_session(session, "game")
    shots = [
        Shot(
            id=f"shot-{i}",
            source_id=skill_game.source_id,
            seconds=10 + i,
            end=1,
            side="red",
            team_delivery=i + 1,
            acceptance="confirmed",
            evidence=[f"skill-sequence:{sequence['id']}:frame:0"],
        )
        for i, sequence in enumerate(sequences)
    ]
    with pytest.raises(ValueError, match="one confirmed shot"):
        save_skills(db, "game", SkillEdit(expected_revision=0, document=SkillDocument(shots=shots)))
    shots[1].acceptance = "excluded"
    saved = save_skills(db, "game", SkillEdit(expected_revision=0, document=SkillDocument(shots=shots)))
    assert saved["revision"] == 1


def test_sequence_rejects_missing_frames_and_post_release_broom():
    with pytest.raises(ValueError, match="2–16"):
        contact_sheets([(1, Image.new("RGB", (10, 10)))])
    with pytest.raises(ValidationError, match="pre-release"):
        DeliveryProposal(
            first_frame=0,
            last_frame=3,
            release_frame=2,
            stone_color="red",
            thrower_description="Gray shirt",
            call="unknown",
            call_basis="",
            broom_frame=3,
            broom_tip=[0.5, 0.5],
            limitations=[],
        )


def test_sequence_request_limits():
    with pytest.raises(ValidationError):
        skills.SequenceRequest(source_id="source", start_seconds=0, end_seconds=60, interval_seconds=0.25)


def test_vision_replay_does_not_charge_again(tmp_path, monkeypatch):
    import io
    import json

    from hogline.perception.vision_budget import budget_status
    from hogline.perception.vision_calls import propose_call

    response = {
        "status": "completed",
        "id": "test-response",
        "usage": {"input_tokens": 100, "output_tokens": 20},
        "output": [
            {
                "content": [
                    {
                        "type": "output_text",
                        "text": json.dumps(
                            {
                                "call": "unknown",
                                "call_basis": "unobserved",
                                "broom_visible": False,
                                "delivery_visible": False,
                                "sweeping_visible": False,
                                "observations": [],
                                "limitations": [],
                            }
                        ),
                    }
                ]
            }
        ],
    }
    calls = []

    def send(request, timeout):
        calls.append(request)
        return io.BytesIO(json.dumps(response).encode())

    monkeypatch.setattr("urllib.request.urlopen", send)
    config = VisionConfig("secret", "gpt-6.1-terra", Decimal(5), True)
    frames = [("frame at second 1", Image.new("RGB", (100, 100)))]
    first = propose_call(tmp_path, frames, config=config)
    assert propose_call(tmp_path, frames, config=config) == first
    assert len(calls) == 1
    assert budget_status(tmp_path / "skill-vision")["committed_cents"] == 1


def test_uncertain_vision_failure_keeps_reservation_and_blocks_retry(tmp_path, monkeypatch):
    import urllib.error

    from hogline.perception.vision_budget import budget_status
    from hogline.perception.vision_calls import propose_call

    calls = []

    def send(request, timeout):
        calls.append(request)
        raise urllib.error.URLError("network unavailable")

    monkeypatch.setattr("urllib.request.urlopen", send)
    config = VisionConfig("secret", "gpt-6.1-terra", Decimal(5), True)
    frames = [("frame at second 1", Image.new("RGB", (100, 100)))]
    with pytest.raises(ValueError, match="reservation retained"):
        propose_call(tmp_path, frames, config=config)
    with pytest.raises(ValueError, match="unresolved prior attempt"):
        propose_call(tmp_path, frames, config=config)
    assert len(calls) == 1
    assert budget_status(tmp_path / "skill-vision")["committed_cents"] == 100
