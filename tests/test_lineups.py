import numpy as np
import pytest
from PIL import Image
from sqlalchemy import select

from hogline.analytics.biometrics import read_archive
from hogline.db import GameLineup, Job, LineupSegment
from hogline.orchestration import lineups, worker

DIM = 16
PEOPLE = np.linalg.qr(np.random.default_rng(7).normal(size=(DIM, DIM)))[0][:6]


def visible(index, view):
    """Deterministic synthetic schedule: people 0-3 on one team, 4-5 on the other."""
    base = [0, 1, 2] if view == "perspective_left" else [3, 4, 5]
    return [p for p in base if (index + p) % 3 != 0]


class FakeFaces:
    version = "fake-face-v1"
    dimensions = DIM

    def __init__(self):
        self.rng = np.random.default_rng(1)

    def observe(self, rgb, people, **_):
        index = int(rgb[0, 0, 0])
        view = "perspective_left" if rgb[0, 0, 1] == 10 else "perspective_right"
        faces = []
        for person in visible(index, view):
            vector = PEOPLE[person] + self.rng.normal(scale=0.08, size=DIM)
            faces.append(
                {
                    "person_index": person,
                    "bbox_px": [10.0 * person, 50.0, 12.0, 12.0],
                    "person_bbox_px": [10.0 * person, 40.0, 30.0, 120.0],
                    "raw_detector_score": 0.9,
                    "eye_px": 7.0 + person,
                    "yaw_proxy": 0.0,
                    "sharpness": 100.0,
                    "foot_x": 0.5,
                    "foot_y": 0.6,
                    "torso_hsv_histogram": None,
                    "embedding": (vector / np.linalg.norm(vector)).astype(np.float32),
                    "thumbnail": np.full((64, 64, 3), person * 30, dtype=np.uint8),
                }
            )
        return faces, {"people": len(people), "faces": len(faces)}


@pytest.fixture
def fake_lineup(monkeypatch):
    def decode(media, start, end, *, interval, max_frames, timeout, tick, source_time_origin, keyframes_only):
        assert not keyframes_only and media == "fake://video"
        t = start
        while t < end - 1e-9:
            index = int(round(t))
            image = np.zeros((1080, 1920, 3), dtype=np.uint8)
            image[:, :, 0] = index % 256
            image[:, : int(1920 * 0.375), 1] = 10
            yield (
                Image.fromarray(image),
                {"pts": int(round(t * 1000)), "time_base_num": 1, "time_base_den": 1000},
            )
            t += interval

    class People:
        def detect(self, rgb):
            return [{"bbox_px": [0, 0, 10, 10]}]

    faces = FakeFaces()
    monkeypatch.setattr(lineups, "decode_scoring_segment", decode)
    monkeypatch.setattr(lineups, "resolve_media", lambda source: {"uri": "fake://video", "format_id": "fake"})
    monkeypatch.setattr(lineups, "optional_person_detector", lambda root: (People(), {"status": "available"}))
    monkeypatch.setattr(lineups, "installed_face_version", lambda root, key=None: ("fake", faces.version))
    monkeypatch.setattr(
        lineups, "optional_face_models", lambda root, key=None: (faces, {"status": "available"})
    )
    return faces


def run_all(db, limit=50):
    for _ in range(limit):
        outcome = worker.run_one(db)
        if outcome is None:
            return
        assert outcome["state"] == "succeeded", outcome["error"]
    raise AssertionError("Work did not drain")


def make_lineup(db, sources, **changes):
    fields = {"source_id": sources[1]["id"], "start_seconds": 100, "end_seconds": 220, "segment_seconds": 30}
    request = lineups.LineupRequest(**{**fields, **changes})
    batch = lineups.create_lineup_batch(db, request)
    run_all(db)
    with db.snapshot_session() as session:
        rows = list(session.scalars(select(GameLineup).where(GameLineup.batch_id == batch["id"])))
    assert len(rows) == 1
    return batch, rows[0].document


@pytest.mark.usefixtures("fake_lineup")
def test_lineup_segments_store_biometrics_and_group_six_people(db, sources):
    batch, lineup = make_lineup(db, sources)
    assert [u["request"]["start_seconds"] for u in batch["units"]] == [100, 120, 150, 180, 210]
    players = [p for p in lineup["players"] if p["role"] == "player"]
    assert len(players) == 6
    assert all(p["span_seconds"] >= 60 for p in players)
    with db.snapshot_session() as session:
        segments = list(session.scalars(select(LineupSegment)))
        jobs = {j.document["kind"]: j.state for j in session.scalars(select(Job))}
    assert jobs == {"lineup_segment": "succeeded", "lineup_assembly": "succeeded"}
    with db.snapshot_session() as session:
        # Every job carries a source span; the workbench job list reads it.
        assert all(
            j.document["request"]["source_id"] == sources[1]["id"] for j in session.scalars(select(Job))
        )
    assert sum(s.document["face_count"] for s in segments) == lineup["face_count"]
    arrays = read_archive(db.settings.data_dir, segments[0].document["archive"])
    assert arrays["embeddings"].dtype == np.float16 and arrays["thumbnails"].shape[1:] == (64, 64, 3)
    # Templates stay in the biometric store, never in catalog documents.
    assert "embedding" not in str(lineup) and (db.settings.data_dir / "biometrics").is_dir()
    templates = read_archive(db.settings.data_dir, lineup["templates"])["templates"]
    assert templates.shape == (len(lineup["players"]), DIM)


def by_number(lineup, number):
    return next(p for p in lineup["players"] if p["number"] == number)


def views(db):
    from hogline.domain.lineups import lineups_in_session

    with db.snapshot_session() as session:
        return lineups_in_session(session, db.settings.data_dir, history=True)


@pytest.mark.usefixtures("fake_lineup")
def test_naming_suggestions_team_games_and_deletion(db, sources):
    from fastapi.testclient import TestClient

    from hogline.domain.catalog import Conflict
    from hogline.domain.lineups import (
        NewPerson,
        PlayerLink,
        Review,
        delete_lineup,
        link_player,
        unlink_player,
    )
    from hogline.review.app import create_app

    _, first = make_lineup(db, sources)
    _, second = make_lineup(db, sources, start_seconds=400, end_seconds=520)
    a, b = first["players"][0], first["players"][1]
    link_player(db, a["id"], PlayerLink(new_person=NewPerson(name=" Matt ", team="Team M"), position="skip"))
    matt = next(p for p in views(db)["people"] if p["name"] == "Matt")
    link_player(db, b["id"], PlayerLink(new_person=NewPerson(name="Pat", team="Team M")))
    with pytest.raises(Conflict):
        link_player(db, a["id"], PlayerLink(person_id=matt["id"]))  # stale: link already exists
    view = views(db)
    second_view = next(x for x in view["lineups"] if x["id"] == second["id"])
    # The same synthetic person in the other game is suggested, strongly, as Matt.
    top = [p for p in second_view["players"] if p["suggestions"] and p["suggestions"][0]["name"] == "Matt"]
    assert len(top) == 1 and top[0]["suggestions"][0]["strength"] == "likely"
    assert top[0]["suggestions"][0]["team"] == "Team M"
    assert all(len(p["suggestions"]) <= 3 for p in second_view["players"])
    (team,) = view["teams"]
    assert team["team"] == "Team M" and [g["lineup_id"] for g in team["games"]] == [first["id"]]
    assert team["games"][0]["confidence"] == "team_game" and team["games"][0]["members_present"] == [
        "Matt",
        "Pat",
    ]
    link_player(db, top[0]["id"], PlayerLink(person_id=matt["id"]))
    team = views(db)["teams"][0]
    assert [g["confidence"] for g in team["games"]] == ["team_game", "one_member_only"]
    client = TestClient(create_app(db.settings))
    api = client.get("/api/lineups").json()
    assert api["face_models"]["yunet"]["role"] == "detector" and len(api["lineups"]) == 2
    assert "templates" not in str(api["lineups"][0]["players"])
    strip = client.get(f"/api/lineups/{first['id']}/players/{a['id']}/faces.jpg?limit=4&size=40")
    assert strip.headers["content-type"] == "image/jpeg"
    from io import BytesIO

    assert Image.open(BytesIO(strip.content)).size == (160, 40)
    more = client.get(f"/api/lineups/people/{matt['id']}/more").json()
    assert more == []  # every strong candidate is already named
    assert client.post(f"/api/lineups/{first['id']}/regroup", json={}).status_code == 409
    revision = views(db)["lineups"][0]["players"][0]["link"]["revision"]
    unlink_player(db, a["id"], revision, Review())
    link_player(db, a["id"], PlayerLink(person_id=matt["id"]))  # history numbering continues
    history = views(db)["history"]
    assert [h["number"] for h in history if h["kind"] == "link" and h["entity_id"] == a["id"]] == [1, 2, 3]
    archives = [s["id"] for s in first["segments"]]
    result = delete_lineup(db, first["id"], Review(reason="Forget this game"))
    assert result["biometric_archives_removed"] == 1 + len(archives)
    view = views(db)
    assert [x["id"] for x in view["lineups"]] == [second["id"]]
    assert not list((db.settings.data_dir / "biometrics/lineup-segments").glob(f"{archives[0]}.npz"))
    # Recreating the same game observes it again instead of reusing deleted output.
    _, again = make_lineup(db, sources)
    assert again["id"] != first["id"] and len(again["players"]) >= 6


@pytest.mark.usefixtures("fake_lineup")
def test_regroup_replaces_unnamed_lineup_and_reports_failures(db, sources):
    from fastapi.testclient import TestClient

    from hogline.review.app import create_app

    batch, lineup = make_lineup(db, sources)
    client = TestClient(create_app(db.settings))
    response = client.post(f"/api/lineups/{lineup['id']}/regroup", json={"distance_threshold": 0.85})
    assert response.status_code == 200
    run_all(db)
    with db.snapshot_session() as session:
        rows = list(session.scalars(select(GameLineup).where(GameLineup.batch_id == batch["id"])))
    assert len(rows) == 1 and rows[0].document["params"]["distance_threshold"] == 0.85
    assert rows[0].id != lineup["id"]
    assert not (db.settings.data_dir / lineup["templates"]["path"]).exists()


def test_grouping_never_merges_simultaneous_faces_and_attaches_small_faces():
    from hogline.reconstruction.lineups import group_faces

    rng = np.random.default_rng(3)
    people = PEOPLE[:3]
    vectors, meta = [], []
    for sample in range(40):
        for person in range(3):
            v = people[person] + rng.normal(scale=0.05, size=DIM)
            vectors.append(v / np.linalg.norm(v))
            meta.append(
                {"segment_id": "s", "sample_index": sample, "view": "v", "eye_px": 8.0 - (sample % 2) * 3}
            )
    # A look-alike twin of person 0 appears at the same instants: never the same group.
    for sample in range(40):
        v = people[0] + rng.normal(scale=0.05, size=DIM)
        vectors.append(v / np.linalg.norm(v))
        meta.append({"segment_id": "s", "sample_index": sample, "view": "v", "eye_px": 7.0})
    labels, info = group_faces(np.array(vectors), meta, {"max_grouped_faces": 120, "attach_similarity": 0.3})
    groups = {}
    for face, label in zip(meta, labels, strict=True):
        if label >= 0:
            key = (face["segment_id"], face["sample_index"], face["view"])
            assert key not in groups.setdefault(label, set())
            groups[label].add(key)
    assert info["grouped"] == 120 and info["faces"] == 160
    again, _ = group_faces(np.array(vectors), meta, {"max_grouped_faces": 120, "attach_similarity": 0.3})
    assert (again == labels).all()
    assert group_faces(np.zeros((0, DIM)), [])[1]["faces"] == 0


def test_face_gating_keeps_one_on_ice_face_per_head(monkeypatch):
    from hogline.perception import faces as face_module

    model = object.__new__(face_module.FaceModels)
    model.dimensions = 4

    def detect(patch):
        h, w = patch.shape[:2]
        size = w * 0.3
        row = [w * 0.35, h * 0.1, size, size, *[0.0] * 10, 0.95]
        lm = [
            (w * 0.42, h * 0.2),
            (w * 0.58, h * 0.2),
            (w * 0.5, h * 0.28),
            (w * 0.44, h * 0.34),
            (w * 0.56, h * 0.34),
        ]
        row[4:14] = [c for point in lm for c in point]
        return [np.asarray(row, dtype=np.float32)]

    model.detect = detect
    model.embed = lambda aligned: np.eye(4, dtype=np.float32)[: len(aligned)]
    rgb = np.random.default_rng(0).integers(0, 255, (1026, 720, 3), dtype=np.uint8)
    people = [
        {"bbox_px": [100, 500, 60, 300], "torso_hsv_histogram": [1.0]},  # on ice
        {"bbox_px": [101, 501, 60, 300]},  # duplicate body proposal
        {"bbox_px": [400, 100, 40, 150]},  # spectator behind the glass
        {"bbox_px": [600, 600, 8, 30]},  # too small for a usable face
    ]
    found, counts = model.observe(rgb, people, min_eye_px=6, min_foot_y=0.4)
    assert counts["off_ice_people"] == 1 and counts["duplicate_faces"] == 1 and counts["below_quality"] == 1
    assert len(found) == 1 and found[0]["person_index"] in (0, 1)
    assert found[0]["thumbnail"].shape == (64, 64, 3) and found[0]["torso_hsv_histogram"] in ([1.0], None)
    assert face_module.head_patch([0, 0, 0, 10], 100, 100) is None
    region, scale = face_module.head_patch([10, 10, 20, 150], 720, 1026)
    assert 1 <= scale <= face_module.MAX_UPSCALE and region[1] == 0


def test_face_models_are_explicit_and_checksum_pinned(tmp_path, monkeypatch):
    from hogline.perception import faces as face_module

    status = face_module.face_model_status(tmp_path)
    assert not any(v["installed"] for v in status.values())
    assert face_module.optional_face_models(tmp_path)[1]["status"] == "unavailable"
    with pytest.raises(ValueError, match="hogline face-models --install"):
        face_module.installed_face_version(tmp_path)

    class Fake:
        def __enter__(self):
            return io.BytesIO(b"not a model")

        def __exit__(self, *_):
            return False

    import io

    monkeypatch.setattr(face_module.urllib.request, "urlopen", lambda *a, **k: Fake())
    with pytest.raises(ValueError, match="checksum"):
        face_module.install_face_models(tmp_path, keys=("yunet",))
    assert not (tmp_path / "models" / face_module.MODELS["yunet"]["file"]).exists()


def test_lineup_requests_are_bounded(db, sources):
    with pytest.raises(ValueError):
        lineups.LineupRequest(source_id="x", start_seconds=0, end_seconds=7 * 3600)
    request = lineups.LineupRequest(source_id="x", start_seconds=95, end_seconds=400, segment_seconds=300)
    assert lineups.segment_bounds(request) == [(95.0, 300.0), (300.0, 400.0)]


def test_merge_joins_unseen_together_fragments_but_not_co_present_people():
    from hogline.reconstruction.lineups import merge_groups

    rng = np.random.default_rng(5)
    base = PEOPLE[0]
    near = base + 0.35 * PEOPLE[1]  # the same person's close-up faces embed a little differently
    near /= np.linalg.norm(near)
    vectors, meta, labels = [], [], []
    for sample in range(60):  # group 0: distant faces, samples 0-59
        vectors.append(base + rng.normal(scale=0.02, size=DIM))
        meta.append({"segment_id": "s", "sample_index": sample, "view": "v", "eye_px": 7.0})
        labels.append(0)
    for sample in range(60, 80):  # group 1: close-ups, never in the same frame as group 0
        vectors.append(near + rng.normal(scale=0.02, size=DIM))
        meta.append({"segment_id": "s", "sample_index": sample, "view": "v", "eye_px": 16.0})
        labels.append(1)
    mate = 0.75 * base + 0.66 * PEOPLE[2]  # similar-looking teammate (cosine ~0.75), on the ice with group 0
    for sample in range(40):
        vectors.append(mate + rng.normal(scale=0.02, size=DIM))
        meta.append({"segment_id": "s", "sample_index": sample, "view": "v", "eye_px": 8.0})
        labels.append(2)
    vectors = np.array(vectors)
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    params = {"merge_similarity": 0.7, "merge_min_faces": 10, "merge_cooccurrence_tolerance": 0.01}
    merged, count = merge_groups(np.array(labels), vectors, meta, params)
    assert count == 1
    assert len(set(merged[:80])) == 1 and set(merged[80:]) != set(merged[:80])


def test_one_camera_edge_groups_are_marked_as_another_sheet():
    from hogline.reconstruction.lineups import DEFAULTS, classify_players

    def group(faces, left, right, x, span=1500):
        return {
            "faces": faces,
            "views": {"perspective_left": left, "perspective_right": right},
            "median_foot_x": x,
            "span_seconds": span,
        }

    groups = [group(600, 300, 300, 0.5) for _ in range(6)]
    groups += [
        group(60, 0, 60, 0.95),
        group(70, 40, 30, 0.5),
        group(80, 40, 40, 0.5, span=30),
        group(20, 10, 10, 0.5),
    ]
    info = classify_players(groups, 1800, DEFAULTS)
    # Floor: 10% of the median of the eight largest groups (600 x6, 80, 70 -> 600).
    assert info["player_face_floor"] == 60.0
    assert [g["role"] for g in groups[6:]] == ["other_sheet", "player", "minor", "minor"]


@pytest.mark.usefixtures("fake_lineup")
def test_rescan_replaces_one_lineup_and_reacquires_faces_atomically(db, sources, monkeypatch):
    from fastapi.testclient import TestClient

    from hogline.review.app import create_app

    # Use a production-valid embedder label with the deterministic fake implementation.
    monkeypatch.setattr(lineups, "installed_face_version", lambda root, key=None: ("sface", "fake-face-v1"))
    batch, original = make_lineup(db, sources)
    old_jobs = {u["job_id"] for u in batch["units"]}
    with TestClient(create_app(db.settings)) as client:
        response = client.post("/api/lineups/" + original["id"] + "/rescan", json={})
        assert response.status_code == 200, response.text
        assert old_jobs.isdisjoint({u["job_id"] for u in response.json()["units"]})
    with db.session() as session:
        assert session.get(GameLineup, original["id"]) is None
    run_all(db)
    with db.session() as session:
        assert len(list(session.scalars(select(GameLineup)))) == 1


@pytest.mark.usefixtures("fake_lineup")
def test_rescan_rolls_back_deletion_when_queueing_fails(db, sources, monkeypatch):
    from fastapi.testclient import TestClient

    from hogline.review.app import create_app

    monkeypatch.setattr(lineups, "installed_face_version", lambda root, key=None: ("sface", "fake-face-v1"))
    _, original = make_lineup(db, sources)

    def fail(*args, **kwargs):
        raise ValueError("Simulated admission failure")

    monkeypatch.setattr(lineups, "lineup_batch_in_session", fail)
    with TestClient(create_app(db.settings)) as client:
        response = client.post("/api/lineups/" + original["id"] + "/rescan", json={})
        assert response.status_code == 422
    with db.session() as session:
        assert session.get(GameLineup, original["id"]) is not None
        assert len(list(session.scalars(select(LineupSegment)))) > 0
    assert read_archive(db.settings.data_dir, original["templates"]) is not None
