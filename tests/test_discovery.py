import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from hogline.adapters import sources as adapter
from hogline.db import DiscoveryFeed, DiscoveryMembership, DiscoveryRun, Job, Source
from hogline.orchestration import discovery
from hogline.orchestration.discovery import (
    FeedControl,
    FeedCreate,
    control_feed,
    create_feed,
    feeds,
    run_discovery_once,
)
from hogline.review.app import create_app

URL = "https://www.youtube.com/channel/UCpQIZCWomj9fy287uBS3cxA/streams"


def raw_entry(index):
    return {
        "id": f"{index:011d}",
        "title": f"SHEET 5 | 01-16-26 · {index}",
        "duration": 1000,
        "http_headers": {"Cookie": "private-fixture"},
        "url": "https://media.invalid/temporary-format-location",
    }


@pytest.fixture
def listing(monkeypatch):
    calls = []
    videos = [raw_entry(i) for i in range(1, 10)]

    def fetch(args, timeout=60):
        spec = args[args.index("--playlist-items") + 1]
        start, end = map(int, spec.split(":"))
        calls.append((start, end))
        assert "--flat-playlist" in args and "--skip-download" in args and timeout == 120
        return {"entries": videos[start - 1 : end]}

    monkeypatch.setattr(adapter, "run_json", fetch)
    return videos, calls


def test_adapter_bounds_sanitizes_and_validates_listing_urls(listing, monkeypatch):
    assert adapter.inventory_url("https://youtube.com/@club") == "https://www.youtube.com/@club/streams"
    for bad in [
        "http://youtube.com/@club/streams",
        "https://youtube.com/watch?v=AAAAAAAAAAA",
        "https://user@youtube.com/@club",
        "https://example.com/@club/streams",
    ]:
        with pytest.raises(ValueError):
            adapter.inventory_url(bad)
    page = adapter.listing_page(URL, start=2, limit=3)
    assert page["raw_count"] == 3 and not page["visible_end_observed"]
    assert "temporary-format-location" not in json.dumps(page) and "Cookie" not in json.dumps(page)
    monkeypatch.setattr(
        adapter, "run_json", lambda *a, **k: {"entries": [None, {"id": "invalid"}, raw_entry(1)]}
    )
    page = adapter.listing_page(URL, limit=4)
    assert len(page["skipped"]) == 2 and page["raw_count"] == 3 and page["visible_end_observed"]
    monkeypatch.setattr(adapter, "run_json", lambda *a, **k: {"id": "AAAAAAAAAAA"})
    with pytest.raises(ValueError, match="listing page"):
        adapter.listing_page(URL)


def test_overlapping_pages_resume_without_media_jobs(db, listing):
    videos, calls = listing
    feed = create_feed(db, FeedCreate(url=URL, page_size=4, overlap=1, backfill_pages_per_cycle=2))
    assert create_feed(db, FeedCreate(url=URL))["id"] == feed["id"]
    assert run_discovery_once(db)["state"] == "succeeded"
    assert run_discovery_once(db)["state"] == "succeeded"
    assert run_discovery_once(db) is None
    assert calls == [(1, 4), (4, 7)]
    current = feeds(db)[0]
    assert current["discovered_sources"] == 7 and current["next_index"] == 7
    control_feed(db, feed["id"], FeedControl(action="next"))
    assert run_discovery_once(db)["state"] == "succeeded"
    assert feeds(db)[0]["backfill_state"] == "visible_end_observed"
    assert feeds(db)[0]["discovered_sources"] == 9
    with db.session() as session:
        assert session.scalar(select(func.count()).select_from(Job)) == 0
        assert session.scalar(select(func.count()).select_from(DiscoveryMembership)) == 9
        assert all(s.document["metadata_completeness"] == "listing" for s in session.scalars(select(Source)))
    assert not (db.settings.data_dir / "evidence").exists()


def test_page_drift_and_repeated_page_cannot_claim_continuity(db, listing, monkeypatch):
    feed = create_feed(db, FeedCreate(url=URL, page_size=4, overlap=1, backfill_pages_per_cycle=1))
    run_discovery_once(db)
    listing[0][:] = [raw_entry(i) for i in range(100, 110)]
    control_feed(db, feed["id"], FeedControl(action="next"))
    run_discovery_once(db)
    assert feeds(db)[0]["backfill_state"] == "needs_reconcile"
    assert feeds(db)[0]["runs"][0]["continuity_supported"] is False
    with pytest.raises(ValueError, match="restart"):
        control_feed(db, feed["id"], FeedControl(action="next"))
    control_feed(db, feed["id"], FeedControl(action="restart"))
    run_discovery_once(db)
    first = listing[0][:4]
    monkeypatch.setattr(adapter, "run_json", lambda *a, **k: {"entries": first})
    control_feed(db, feed["id"], FeedControl(action="next"))
    run_discovery_once(db)
    assert feeds(db)[0]["backfill_state"] == "needs_reconcile"


def test_acquired_page_survives_interrupted_import_and_retry_backoff(db, listing, monkeypatch):
    feed = create_feed(db, FeedCreate(url=URL, page_size=4, overlap=1, backfill_pages_per_cycle=1))
    original = discovery.import_youtube

    def interrupt(*args, **kwargs):
        raise ValueError("Injected import interruption")

    monkeypatch.setattr(discovery, "import_youtube", interrupt)
    assert run_discovery_once(db)["state"] == "failed"
    assert run_discovery_once(db) is None
    with db.session() as session:
        row = session.scalar(select(DiscoveryRun))
        assert row.document["response"]["sha256"]
        row.document = {**row.document, "retry_at": ""}
    monkeypatch.setattr(discovery, "import_youtube", original)
    assert run_discovery_once(db)["state"] == "succeeded"
    assert len(listing[1]) == 1
    assert feeds(db)[0]["id"] == feed["id"]


def test_pause_reconcile_head_and_superseded_running_page(db, listing):
    feed = create_feed(
        db, FeedCreate(url=URL, page_size=4, overlap=1, scheduled=True, backfill_pages_per_cycle=1)
    )
    control_feed(db, feed["id"], FeedControl(action="pause"))
    assert run_discovery_once(db) is None
    control_feed(db, feed["id"], FeedControl(action="resume"))
    run = discovery.claim_discovery(db)
    control_feed(db, feed["id"], FeedControl(action="restart"))
    discovery.execute_discovery(db, run)
    assert feeds(db)[0]["next_index"] == 1
    assert feeds(db)[0]["runs"][1]["superseded"] is True
    run_discovery_once(db)
    cursor = feeds(db)[0]["next_index"]
    with db.session() as session:
        row = session.get(DiscoveryFeed, feed["id"])
        row.document = {**row.document, "next_head_at": "2000-01-01T00:00:00+00:00"}
    run_discovery_once(db)
    assert feeds(db)[0]["runs"][0]["request"]["mode"] == "head"
    assert feeds(db)[0]["next_index"] == cursor


def test_mid_fetch_pause_keeps_page_and_restart_clears_old_anchors(db, listing, monkeypatch):
    feed = create_feed(db, FeedCreate(url=URL, page_size=4, overlap=1, backfill_pages_per_cycle=1))
    original = adapter.run_json

    def pause_then_return(*args, **kwargs):
        control_feed(db, feed["id"], FeedControl(action="pause"))
        control_feed(db, feed["id"], FeedControl(action="pause"))  # idempotent control
        return original(*args, **kwargs)

    monkeypatch.setattr(adapter, "run_json", pause_then_return)
    assert run_discovery_once(db)["state"] == "queued"
    assert feeds(db)[0]["discovered_sources"] == 0
    assert feeds(db)[0]["runs"][0]["attempts"] == 0
    control_feed(db, feed["id"], FeedControl(action="resume"))
    monkeypatch.setattr(adapter, "run_json", original)
    run_discovery_once(db)
    assert len(listing[1]) == 1
    control_feed(db, feed["id"], FeedControl(action="restart"))
    run_discovery_once(db)
    assert feeds(db)[0]["backfill_state"] == "ready"
    assert feeds(db)[0]["next_index"] == 4


def test_completed_reconciliation_reports_absence_without_deleting_source(db, listing):
    feed = create_feed(db, FeedCreate(url=URL, page_size=4, overlap=1, backfill_pages_per_cycle=3))
    for _ in range(3):
        assert run_discovery_once(db)["state"] == "succeeded"
    assert feeds(db)[0]["discovered_sources"] == 9
    listing[0][:] = listing[0][:3]
    control_feed(db, feed["id"], FeedControl(action="restart"))
    run_discovery_once(db)
    result = feeds(db)[0]
    assert len(result["unseen_in_visible_pass_source_ids"]) == 6
    assert result["discovered_sources"] == 9
    with db.session() as session:
        assert session.scalar(select(func.count()).select_from(Source)) == 9
        unseen = session.get(Source, result["unseen_in_visible_pass_source_ids"][0]).document
        assert unseen["availability"] == "unknown"
        assert unseen["metadata_refresh_needed"] is True


def test_discovery_api_exposes_curated_page_snapshot(db, listing):
    with TestClient(create_app(db.settings)) as client:
        body = {"url": URL, "page_size": 4, "overlap": 1, "backfill_pages_per_cycle": 1}
        feed = client.post("/api/discovery/feeds", json=body).json()
        run_discovery_once(db)
        response = client.get("/api/discovery").json()
        run = response["feeds"][0]["runs"][0]
        assert "response" not in run
        saved = client.get(f"/api/discovery/runs/{run['id']}").json()
        assert saved["response"]["sha256"] == saved["response_sha256"]
        assert "Cookie" not in json.dumps(saved)
        assert (
            client.post(
                f"/api/discovery/feeds/{feed['id']}/control", json={"action": "configure", "scheduled": True}
            ).json()["config"]["scheduled"]
            is True
        )
        assert client.get("/api/discovery/runs/missing").status_code == 404


def test_expired_worker_cannot_refetch_or_advance(db, listing):
    create_feed(db, FeedCreate(url=URL, page_size=4, overlap=1, backfill_pages_per_cycle=1))
    first = discovery.claim_discovery(db)
    with db.session() as session:
        session.get(DiscoveryRun, first["id"]).lease_until = (
            datetime.now(UTC) - timedelta(seconds=1)
        ).isoformat()
    second = discovery.claim_discovery(db)
    assert first["lease_token"] != second["lease_token"]
    with pytest.raises(ValueError, match="reclaimed"):
        discovery.execute_discovery(db, first)
    assert listing[1] == []
    discovery.execute_discovery(db, second)
    assert feeds(db)[0]["next_index"] == 4


def test_retry_limit_requires_attention_and_restart(db, listing, monkeypatch):
    feed = create_feed(db, FeedCreate(url=URL, page_size=4, overlap=1, backfill_pages_per_cycle=1))
    original = adapter.run_json
    calls = []

    def unavailable(*args, **kwargs):
        calls.append(1)
        raise ValueError("Injected transient listing failure")

    monkeypatch.setattr(adapter, "run_json", unavailable)
    for _ in range(3):
        assert run_discovery_once(db)["state"] == "failed"
        assert run_discovery_once(db) is None
        with db.session() as session:
            row = session.scalar(select(DiscoveryRun))
            row.document = {**row.document, "retry_at": ""}
    assert len(calls) == 3
    assert feeds(db)[0]["backfill_state"] == "needs_attention"
    assert run_discovery_once(db) is None
    monkeypatch.setattr(adapter, "run_json", original)
    control_feed(db, feed["id"], FeedControl(action="restart"))
    assert run_discovery_once(db)["state"] == "succeeded"


def test_position_scope_limit_is_not_a_listing_end(listing, monkeypatch):
    def last_positions(*args, **kwargs):
        return {"entries": [raw_entry(9999), raw_entry(10000)]}

    monkeypatch.setattr(adapter, "run_json", last_positions)
    page = adapter.listing_page(URL, start=9999, limit=4)
    assert page["index_limit_reached"] is True
    assert page["visible_end_observed"] is False


def test_scheduled_visible_end_starts_new_bounded_generation(db, listing):
    feed = create_feed(
        db, FeedCreate(url=URL, page_size=4, overlap=1, backfill_pages_per_cycle=3, scheduled=True)
    )
    for _ in range(3):
        run_discovery_once(db)
    assert feeds(db)[0]["backfill_state"] == "visible_end_observed"
    with db.session() as session:
        row = session.get(DiscoveryFeed, feed["id"])
        row.document = {**row.document, "next_head_at": "2000-01-01T00:00:00+00:00"}
    assert run_discovery_once(db)["state"] == "succeeded"
    current = feeds(db)[0]
    assert current["generation"] == 2
    assert current["next_index"] == 4
    assert current["cycle_remaining"] == 2
    assert current["backfill_state"] == "ready"
