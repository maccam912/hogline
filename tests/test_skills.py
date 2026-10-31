import pytest
from pydantic import ValidationError

from hogline.analytics.skills import skill_report
from hogline.db import GameRow
from hogline.domain.catalog import Conflict
from hogline.domain.skills import (
    Shot,
    SkillDocument,
    SkillEdit,
    Slot,
    read_skills,
    role_proposals,
    save_skills,
    skill_history,
)


def shot(**changes):
    return Shot.model_validate(
        {
            "id": "s1",
            "source_id": "source",
            "seconds": 30,
            "end": 1,
            "side": "red",
            "team_delivery": 1,
            "participant": "Red lead",
            "acceptance": "confirmed",
            "evidence": ["frame:30"],
            "call": "draw",
            "direction": "near_to_far",
            "turn": "left",
            "speed_mps": 1.8,
            "target_speed_mps": 2,
            "calibration": "plane-v1",
            "line_error_degrees": 2,
            **changes,
        }
    )


def test_bias_spread_signs_and_single_sample():
    report = skill_report(
        SkillDocument(shots=[shot(), shot(id="s2", seconds=31, team_delivery=2, speed_mps=2.2)])
    )
    group = report["groups"][0]
    assert group["weight"]["mean"] == 1
    assert group["weight"]["sample_sd"] == pytest.approx(0.141421356)
    assert group["line"]["mean"] == group["wide"]["mean"] == 2
    right = skill_report(SkillDocument(shots=[shot(turn="right")]))["groups"][0]
    assert right["wide"]["mean"] == -2
    assert right["weight"]["sample_sd"] is None
    assert right["weight"]["status"] == "insufficient_sample"


def test_unreviewed_unknown_and_uncalibrated_are_not_scores():
    report = skill_report(
        SkillDocument(
            shots=[
                shot(acceptance="proposed"),
                shot(id="s2", seconds=31, team_delivery=2, call="unknown"),
                shot(id="s3", seconds=32, team_delivery=3, calibration=None),
            ]
        )
    )
    assert report["excluded"] == {"proposed": 1, "unknown_call_or_direction": 1}
    assert report["groups"][0]["weight"]["n"] == 0
    assert report["groups"][0]["line"]["n"] == 0
    with pytest.raises(ValidationError):
        shot(speed_mps=float("nan"))
    with pytest.raises(ValidationError):
        shot(evidence=[])


def test_roles_preserve_conflict_and_do_not_infer_skip():
    document = SkillDocument(
        shots=[shot(), shot(id="s2", seconds=31, participant="Someone else", team_delivery=2)]
    )
    assert role_proposals(document)[0]["status"] == "conflict"
    assert "skip" not in role_proposals(document)[0]
    with pytest.raises(ValidationError):
        SkillDocument(
            slots=[
                Slot(end=1, side="red", position="lead", participant="A"),
                Slot(end=1, side="red", position="lead", participant="B"),
            ]
        )


def test_anonymous_slots_only_join_reviewed_end_assignments():
    slots = [Slot(end=1, side="red", position="lead", participant="Player A", acceptance="confirmed")]
    report = skill_report(SkillDocument(slots=slots, shots=[shot(participant=None)]))
    assert report["groups"][0]["participant"] == "Player A"
    conflict = skill_report(SkillDocument(slots=slots, shots=[shot()]))
    assert conflict["excluded"] == {"participant_conflict": 1}


def test_revision_conflicts_history_and_game_bounds(db, sources):
    source_id = sources[0]["id"]
    with db.snapshot_session(write=True) as session:
        session.add(
            GameRow(
                id="game",
                document={"spans": [{"source_id": source_id, "start_seconds": 0, "end_seconds": 100}]},
            )
        )
    edit = SkillEdit(expected_revision=0, document=SkillDocument(shots=[shot(source_id=source_id)]))
    assert save_skills(db, "game", edit)["revision"] == 1
    with pytest.raises(Conflict):
        save_skills(db, "game", edit)
    edit.expected_revision = 1
    edit.document.shots[0].seconds = 200
    with pytest.raises(ValueError, match="source span"):
        save_skills(db, "game", edit)
    with db.snapshot_session() as session:
        assert read_skills(session, "game")["revision"] == 1
        assert len(skill_history(session, "game")) == 1


def test_vision_env_never_exposes_or_executes_key(tmp_path, monkeypatch):
    from hogline.perception.vision_config import KEYS, load_vision_config

    for key in KEYS:
        monkeypatch.delenv(key, raising=False)
    path = tmp_path / ".env"
    path.write_text('OPENAI_API_KEY="literal-$(never-execute)"\nHOGLINE_VISION_ENABLED=true\n')
    config = load_vision_config(path)
    assert config.api_key == "literal-$(never-execute)"
    assert config.public_status()["key_configured"]
    assert "literal" not in str(config.public_status())
    monkeypatch.setenv("HOGLINE_VISION_BUDGET_USD", "NaN")
    with pytest.raises(ValueError):
        load_vision_config(path)


def test_vision_budget_and_provenance(tmp_path, monkeypatch):
    import io
    import json
    from decimal import Decimal

    from PIL import Image

    from hogline.perception.vision_calls import propose_call
    from hogline.perception.vision_config import VisionConfig

    response = {
        "status": "completed",
        "id": "response-test",
        "usage": {"input_tokens": 100},
        "output": [
            {
                "content": [
                    {
                        "type": "output_text",
                        "text": json.dumps(
                            {
                                "call": "unknown",
                                "call_basis": "Single frame",
                                "broom_visible": True,
                                "delivery_visible": False,
                                "sweeping_visible": False,
                                "observations": ["Broom near house"],
                                "limitations": ["Intent unclear"],
                            }
                        ),
                    }
                ]
            }
        ],
    }
    calls = []

    def send(request, timeout):
        calls.append(json.loads(request.data))
        return io.BytesIO(json.dumps(response).encode())

    monkeypatch.setattr("urllib.request.urlopen", send)
    config = VisionConfig("test-secret", "gpt-6.1-terra", Decimal("1"), True)
    result = propose_call(
        tmp_path, [("source:test second:30", Image.new("RGB", (1920, 1080)))], config=config
    )
    assert result["acceptance"] == "proposed"
    assert result["frames"][0]["width"] == 1024
    assert "test-secret" not in json.dumps(result)
    assert calls[0]["store"] is False
    with pytest.raises(ValueError, match="budget reached"):
        propose_call(tmp_path, [("frame", Image.new("RGB", (10, 10)))], config=config)
    assert len(calls) == 1


def test_skill_review_http_round_trip(db, sources):
    from fastapi.testclient import TestClient

    from hogline.review.app import create_app

    source_id = sources[0]["id"]
    with db.snapshot_session(write=True) as session:
        session.add(
            GameRow(
                id="game",
                document={
                    "id": "game",
                    "spans": [{"source_id": source_id, "start_seconds": 0, "end_seconds": 100}],
                },
            )
        )
    client = TestClient(create_app(db.settings))
    catalog = client.get("/api/skills/catalog")
    assert catalog.status_code == 200
    assert len(catalog.json()["sources"]) == 2
    result = client.get("/api/games/game/skills")
    assert result.json()["revision"] == 0
    edit = SkillEdit(expected_revision=0, document=SkillDocument(shots=[shot(source_id=source_id)]))
    assert client.put("/api/games/game/skills", json=edit.model_dump()).status_code == 200
    assert client.put("/api/games/game/skills", json=edit.model_dump()).status_code == 409
    report = client.get("/api/games/game/skills").json()["report"]
    assert report["groups"][0]["weight"]["mean"] == 0.9
    assert client.get("/static/skills.html").status_code == 200


def test_outside_bias_survives_opposite_left_right_errors():
    report = skill_report(
        SkillDocument(
            shots=[
                shot(turn="left", line_error_degrees=2),
                shot(id="s2", seconds=31, team_delivery=2, turn="right", line_error_degrees=-2),
            ]
        )
    )
    assert len(report["groups"]) == 2
    combined = report["summaries"][0]
    assert combined["line"]["mean"] == 0
    assert combined["wide"]["mean"] == 2
    assert combined["wide"]["sample_sd"] == 0
    assert combined["turn_counts"] == {"left": 1, "right": 1}


def test_unknown_people_are_not_pooled_as_one_player():
    report = skill_report(SkillDocument(shots=[shot(participant=None, team_delivery=None)]))
    assert report["excluded"] == {"unknown_participant_and_position": 1}
    assert report["summaries"] == []


def test_unknown_end_side_and_duplicates_cannot_be_confirmed():
    with pytest.raises(ValidationError, match="end and side"):
        shot(end=None, side="unknown")
    with pytest.raises(ValidationError, match="team delivery"):
        SkillDocument(shots=[shot(), shot(id="duplicate", seconds=31)])
    with pytest.raises(ValidationError, match="source moment"):
        SkillDocument(shots=[shot(), shot(id="duplicate", team_delivery=2)])


def test_game_changes_invalidate_skill_statistics(db, sources):
    from fastapi.testclient import TestClient

    from hogline.review.app import create_app

    sid = sources[0]["id"]
    with db.snapshot_session(write=True) as session:
        session.add(
            GameRow(
                id="game", document={"spans": [{"source_id": sid, "start_seconds": 0, "end_seconds": 100}]}
            )
        )
    save_skills(
        db, "game", SkillEdit(expected_revision=0, document=SkillDocument(shots=[shot(source_id=sid)]))
    )
    with db.snapshot_session(write=True) as session:
        row = session.get(GameRow, "game")
        row.document = {**row.document, "note": "boundaries reconsidered"}
    result = TestClient(create_app(db.settings)).get("/api/games/game/skills").json()
    assert result["stale"]
    assert result["report"]["groups"] == []
    assert result["report"]["excluded"] == {"stale_game_or_source": 1}
