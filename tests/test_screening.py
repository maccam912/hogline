import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from hogline.db import Job, Source
from hogline.review.app import create_app


@pytest.fixture
def preview(db, sources):
    folder = db.settings.data_dir / "screening"
    folder.mkdir()
    content = b"preview fixture"
    sha = hashlib.sha256(content).hexdigest()
    (folder / f"{sha}.jpg").write_bytes(content)
    item = {
        "source_id": sources[0]["id"],
        "metadata_sha256": sources[0]["metadata_sha256"],
        "source_seconds": 60,
        "sha256": sha,
        "date": "2026-03-08",
    }
    (folder / "manifest.json").write_text(json.dumps({"previews": [item]}))
    return item


def test_manual_preview_label_is_revisioned_and_enqueues_no_analysis(db, preview):
    client = TestClient(create_app(db.settings))
    path = f"/api/screening/{preview['source_id']}/label"
    body = {
        "decision": "team_visible",
        "team": "Koski Team",
        "expected_revision": 0,
        "expected_preview_sha256": preview["sha256"],
    }
    response = client.put(path, json=body)
    assert response.status_code == 200 and response.json()["scope"] == "this_preview_only"
    assert client.put(path, json=body).status_code == 409
    assert client.get("/api/screening").json()["previews"][0]["label"]["revision"] == 1
    with db.session() as session:
        assert session.query(Job).count() == 0
        source = session.get(Source, preview["source_id"])
        assert source.document["metadata_sha256"] == preview["metadata_sha256"]
        assert len(source.document["preview_label_history"]) == 1
    response = client.put(path, json={**body, "decision": "not_visible", "expected_revision": 1})
    assert response.status_code == 200 and response.json()["scope"] == "this_preview_only"


def test_unknown_stale_or_changed_previews_are_not_silently_labeled(db, preview):
    client = TestClient(create_app(db.settings))
    body = {"decision": "team_visible", "expected_revision": 0, "expected_preview_sha256": preview["sha256"]}
    assert client.put("/api/screening/missing/label", json=body).status_code == 422
    with db.session() as session:
        source = session.get(Source, preview["source_id"])
        source.document = {**source.document, "metadata_sha256": "changed"}
    assert client.put(f"/api/screening/{preview['source_id']}/label", json=body).status_code == 409
    image = f"/api/screening/{preview['source_id']}/preview.jpg"
    assert client.get(image).status_code == 200
    (db.settings.data_dir / "screening" / f"{preview['sha256']}.jpg").write_bytes(b"changed")
    assert client.get(image).status_code == 410


def test_changed_preview_requires_new_label_and_rejects_stale_browser(db, preview):
    client = TestClient(create_app(db.settings))
    path = f"/api/screening/{preview['source_id']}/label"
    body = {"decision": "team_visible", "expected_revision": 0, "expected_preview_sha256": preview["sha256"]}
    assert client.put(path, json=body).status_code == 200
    changed = {**preview, "sha256": "a" * 64, "source_seconds": 120}
    (db.settings.data_dir / "screening" / "manifest.json").write_text(json.dumps({"previews": [changed]}))
    item = client.get("/api/screening").json()["previews"][0]
    assert item["label"] is None and item["label_revision"] == 1
    assert client.put(path, json={**body, "expected_revision": 1}).status_code == 409
    response = client.put(
        path, json={**body, "expected_revision": 1, "expected_preview_sha256": changed["sha256"]}
    )
    assert response.status_code == 200 and response.json()["revision"] == 2
    with db.session() as session:
        assert len(session.get(Source, preview["source_id"]).document["preview_label_history"]) == 2


def test_additional_preview_is_scoped_and_does_not_replace_label_target(db, preview):
    folder = db.settings.data_dir / "screening"
    data = b"extra moment"
    sha = hashlib.sha256(data).hexdigest()
    (folder / f"{sha}.jpg").write_bytes(data)
    item = {**preview, "additional_previews": [{"sha256": sha, "source_seconds": 120}]}
    (folder / "manifest.json").write_text(json.dumps({"previews": [item]}))
    client = TestClient(create_app(db.settings))
    path = f"/api/screening/{preview['source_id']}/preview.jpg"
    assert client.get(path, params={"sha": sha}).content == data
    assert client.get(path, params={"sha": "missing"}).status_code == 404
    assert client.get(path).content == b"preview fixture"
    assert (
        client.put(
            f"/api/screening/{preview['source_id']}/label",
            json={
                "decision": "team_visible",
                "expected_revision": 0,
                "expected_preview_sha256": sha,
            },
        ).status_code
        == 409
    )
