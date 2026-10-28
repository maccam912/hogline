import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from hogline.adapters.sources import import_youtube
from hogline.analytics.exports import snapshot
from hogline.db import Job, JobExecution, MetadataSnapshot, Source
from hogline.orchestration import metadata
from hogline.orchestration.metadata import MetadataRefreshRequest, enqueue_refresh, metadata_queue
from hogline.orchestration.worker import cached_frame, claim, execute, retry, run_one
from hogline.review.app import create_app

URL = "https://www.youtube.com/watch?v=CCCCCCCCCCC"


def listed(db, vid="CCCCCCCCCCC"):
    return import_youtube(
        db,
        f"https://www.youtube.com/watch?v={vid}",
        {
            "id": vid,
            "title": "SHEET 2 | 03-17-26",
            "duration": 20,
        },
        metadata_kind="listing",
    )


def response(source):
    return {
        "id": source["platform_id"],
        "title": "SHEET 2 | 03-17-26",
        "duration": 22,
        "formats": [{"format_id": "720", "height": 720, "url": "https://secret.invalid/signed"}],
        "http_headers": {"Cookie": "secret-cookie"},
    }


def test_bounded_selection_deduplicates_and_preserves_metadata_only(db, sources, monkeypatch):
    a, b = listed(db), listed(db, "DDDDDDDDDDD")
    request = MetadataRefreshRequest(limit=1, timeout_seconds=17)
    first = enqueue_refresh(db, request)
    second = enqueue_refresh(db, request)
    assert len(first["jobs"]) == len(second["jobs"]) == 1
    assert first["jobs"][0]["id"] != second["jobs"][0]["id"]
    assert enqueue_refresh(db, request)["jobs"] == []
    calls = []

    def fetch(args, timeout):
        calls.append((args, timeout))
        source = a if a["platform_id"] in args[-1] else b
        return response(source)

    monkeypatch.setattr(metadata, "run_json", fetch)
    assert run_one(db)["state"] == "succeeded"
    assert run_one(db)["state"] == "succeeded"
    assert run_one(db) is None
    assert len(calls) == 2
    assert all(
        "--skip-download" in args and "--ignore-config" in args and timeout == 17 for args, timeout in calls
    )
    with db.session() as session:
        assert metadata_queue(session)["pending_sources"] == []
        current = session.get(Source, a["id"]).document
        assert current["duration_seconds"] == 22
        assert current["metadata_completeness"] == "full"
        manifests = list(session.scalars(select(MetadataSnapshot)))
        assert len([m for m in manifests if m.source_id == a["id"]]) == 2
        assert "signed" not in json.dumps([m.document for m in manifests])
        assert "secret-cookie" not in json.dumps([m.document for m in manifests])
        assert all(e.state == "succeeded" for e in session.scalars(select(JobExecution)))
        assert all(j.document["kind"] == "metadata_refresh" for j in session.scalars(select(Job)))
    # A metadata job must not be mistaken for a legacy frame-acquisition job.
    assert cached_frame(db, a, 0)[0] is None
    release = snapshot(db, "exploratory")
    assert release["evidence"] == []
    assert release["games"] == []


@pytest.mark.parametrize("when", ["before", "during", "lease"])
def test_stale_source_or_lease_cannot_publish(db, monkeypatch, when):
    source = listed(db)
    enqueue_refresh(db, MetadataRefreshRequest())
    job = claim(db)

    def change():
        if when == "lease":
            with db.session() as session:
                row = session.get(Job, job["id"])
                row.document = {**row.document, "lease_token": "another-worker"}
        else:
            import_youtube(
                db, URL, {"id": source["platform_id"], "title": "Newer source title"}, metadata_kind="listing"
            )

    def fetch(*args, **kwargs):
        if when == "before":
            pytest.fail("A stale queued source must not make a network request")
        change()
        return response(source)

    monkeypatch.setattr(metadata, "run_json", fetch)
    if when == "before":
        change()
    with pytest.raises(ValueError, match="changed|reclaimed"):
        execute(db, job)
    with db.session() as session:
        row = session.get(Source, source["id"])
        assert row.document["metadata_completeness"] == "listing"
        assert row.document["duration_seconds"] == 20


def test_failures_preserve_source_and_retries_are_bounded(db, monkeypatch):
    source = listed(db)
    enqueue_refresh(db, MetadataRefreshRequest())
    monkeypatch.setattr(metadata, "run_json", lambda *a, **kw: {"id": "WRONG-ID"})
    first = run_one(db)
    assert first["state"] == "failed"
    with db.session() as session:
        assert session.get(Source, source["id"]).document == source
    # Re-enqueueing the same inputs cannot silently reset failure attempts.
    assert enqueue_refresh(db, MetadataRefreshRequest())["jobs"] == []
    assert enqueue_refresh(db, MetadataRefreshRequest(timeout_seconds=60))["jobs"] == []
    for _ in range(2):
        retry(db, first["id"])
        assert run_one(db)["state"] == "failed"
    with pytest.raises(ValueError, match="Retry limit"):
        retry(db, first["id"])


def test_refresh_api_bounds_and_preview(db, monkeypatch):
    source = listed(db)
    monkeypatch.setattr(metadata, "run_json", lambda *a, **kw: pytest.fail("Enqueue must not fetch"))
    with TestClient(create_app(db.settings)) as client:
        assert client.get("/api/metadata-refresh").json()["pending_sources"][0]["id"] == source["id"]
        assert client.post("/api/metadata-refresh", json={"limit": 17}).status_code == 422
        assert client.post("/api/metadata-refresh", json={"timeout_seconds": 61}).status_code == 422
        result = client.post("/api/metadata-refresh", json={"limit": 1}).json()
        assert len(result["jobs"]) == 1
        assert client.get("/api/catalog").status_code == 200
