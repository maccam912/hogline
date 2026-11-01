from datetime import date

from sqlalchemy import select

from hogline.db import DiscoveryFeed, DiscoveryMembership, Job, ProcessingBatch, Source
from hogline.orchestration.recording_campaigns import (
    CampaignRequest,
    CampaignStart,
    RecordingCampaign,
    control,
    matches,
    preview,
    start,
    tick,
)


def request(**changes):
    return CampaignRequest(start_date="2026-01-01", end_date="2026-01-31", sheets=[5], **changes)


def test_date_scope_is_inclusive_and_skips_unknown_and_excluded_titles():
    req = request(sundays_only=True)
    source = {"title_date": "2026-01-04", "sheet": 5, "title": "Sunday curling"}
    assert matches(source, req, ("junior",))
    assert not matches({**source, "title_date": "2026-01-05"}, req, ())
    assert not matches({**source, "title_date": None}, req, ())
    assert not matches({**source, "title": "Junior Sunday curling"}, req, ("junior",))
    assert not matches({**source, "sheet": 4}, req, ())
    assert req.start_date == date(2026, 1, 1)


def test_campaign_is_durable_idempotent_and_pauses_owned_work(db, sources):
    body = CampaignStart(id="test-campaign", request=request())
    start(db, body)
    start(db, body)
    tick(db)
    with db.session() as session:
        campaign = session.get(RecordingCampaign, body.id)
        assert len(campaign.document["items"]) == 2
        batch_ids = {i["batch_id"] for i in campaign.document["items"].values()}
        assert len(batch_ids) == 2
        assert len(list(session.scalars(select(RecordingCampaign)))) == 1
    tick(db)
    with db.session() as session:
        assert len(list(session.scalars(select(ProcessingBatch)))) == 2
    control(db, body.id, "pause")
    with db.session() as session:
        assert session.get(RecordingCampaign, body.id).state == "paused"
        assert all(session.get(ProcessingBatch, identifier).state == "paused" for identifier in batch_ids)
    control(db, body.id, "resume")
    with db.session() as session:
        assert all(session.get(ProcessingBatch, identifier).state == "active" for identifier in batch_ids)
    control(db, body.id, "cancel")
    with db.session() as session:
        assert all(session.get(ProcessingBatch, identifier).state == "cancelled" for identifier in batch_ids)


def test_existing_analysis_is_reused_but_not_paused_by_campaign(db, sources):
    first = CampaignStart(id="first", request=request())
    start(db, first)
    tick(db)
    second = CampaignStart(id="second", request=request())
    start(db, second)
    tick(db)
    control(db, second.id, "pause")
    with db.session() as session:
        assert all(
            not i["owned"] for i in session.get(RecordingCampaign, second.id).document["items"].values()
        )
        assert all(b.state == "active" for b in session.scalars(select(ProcessingBatch)))


def test_discovered_metadata_is_refreshed_before_analysis_and_listing_continues(db, sources):
    url = "https://www.youtube.com/@test/streams"
    req = request(feed_url=url)
    assert preview(db, req)["items"] == []  # a new feed must not claim unrelated archive matches
    result = start(db, CampaignStart(id="discover", request=req))
    feed_id = result["feed_id"]
    with db.session() as session:
        session.add(DiscoveryMembership(feed_id=feed_id, source_id=sources[1]["id"], document={}))
        source = session.get(Source, sources[1]["id"])
        source.document = {**source.document, "metadata_refresh_needed": True}
        feed = session.get(DiscoveryFeed, feed_id)
        feed.document = {**feed.document, "backfill_state": "visible_end_observed"}
    tick(db)
    with db.session() as session:
        item = session.get(RecordingCampaign, "discover").document["items"][sources[1]["id"]]
        assert item["state"] == "metadata"
        assert not list(session.scalars(select(ProcessingBatch)))
        session.get(Job, item["metadata_job_id"]).state = "succeeded"
        source = session.get(Source, sources[1]["id"])
        source.document = {**source.document, "metadata_refresh_needed": False}
    tick(db)
    with db.session() as session:
        item = session.get(RecordingCampaign, "discover").document["items"][sources[1]["id"]]
        session.get(ProcessingBatch, item["batch_id"]).state = "completed"
    tick(db)
    with db.session() as session:
        assert session.get(RecordingCampaign, "discover").state == "completed"


def test_all_weekdays_are_default_and_friday_can_be_selected():
    req = request()
    assert req.weekdays == list(range(7))
    friday = {"title_date": "2026-01-16", "sheet": 5, "title": "Friday league"}
    assert matches(friday, req, ())
    chosen = request(weekdays=[0, 4])
    assert matches(friday, chosen, ())
    assert not matches({**friday, "title_date": "2026-01-18"}, chosen, ())
    assert request(sundays_only=True).weekdays == [6]  # keep existing saved batches scoped


def test_invalid_or_empty_weekday_selection_is_rejected():
    import pytest

    for days in ([], [7], [-1], [4, 4]):
        with pytest.raises(ValueError):
            request(weekdays=days)


def test_new_channel_undated_listing_gets_metadata_then_date_filter(db, sources):
    req = CampaignRequest(
        start_date="2026-10-01",
        end_date="2026-10-31",
        sheets=[5],
        weekdays=[4],
        feed_url="https://www.youtube.com/@curlingstadiumwausau/streams",
    )
    result = start(db, CampaignStart(id="new-channel", request=req))
    sid = sources[1]["id"]
    with db.session() as session:
        session.add(DiscoveryMembership(feed_id=result["feed_id"], source_id=sid, document={}))
        source = session.get(Source, sid)
        source.document = {
            **source.document,
            "title": "Oct 30 — Friday 6:15 — Koski, Lindsey vs Dobbs, Jason (Sheet 5)",
            "title_date": None,
            "upload_date": None,
            "metadata_refresh_needed": True,
        }
    tick(db)
    with db.session() as session:
        item = session.get(RecordingCampaign, "new-channel").document["items"][sid]
        assert item["state"] == "metadata"
        session.get(Job, item["metadata_job_id"]).state = "succeeded"
        source = session.get(Source, sid)
        source.document = {**source.document, "upload_date": "20261031", "metadata_refresh_needed": False}
    tick(db)
    with db.session() as session:
        item = session.get(RecordingCampaign, "new-channel").document["items"][sid]
        assert item["state"] == "active"
        assert item["batch_id"]
