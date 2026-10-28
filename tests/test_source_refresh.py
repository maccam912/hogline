from sqlalchemy import select

from hogline.adapters.sources import digest, import_youtube
from hogline.db import MetadataSnapshot, Source

URL = "https://www.youtube.com/watch?v=AAAAAAAAAAA"


def test_listing_never_downgrades_full_manifest_or_resets_interpretations(db, sources):
    original = sources[0]
    with db.session() as session:
        row = session.get(Source, original["id"])
        row.document = {
            **row.document,
            "recording_clock_date": "2026-01-16",
            "event_date": "2026-01-16",
            "clock_status": "reviewed_mapping",
        }
    refreshed = import_youtube(
        db, URL, {"id": "AAAAAAAAAAA", "title": "Edited channel title"}, metadata_kind="listing"
    )
    assert refreshed["title"] == original["title"]
    assert refreshed["duration_seconds"] == original["duration_seconds"]
    assert refreshed["upload_date"] == original["upload_date"]
    assert refreshed["metadata_sha256"] == original["metadata_sha256"]
    assert refreshed["metadata_completeness"] == "full"
    assert refreshed["metadata_refresh_needed"] is True
    assert refreshed["clock_status"] == "reviewed_mapping"
    assert refreshed["event_date"] == "2026-01-16"
    assert refreshed["listing_hints"]["title"] == "Edited channel title"
    with db.session() as session:
        for snap in session.scalars(select(MetadataSnapshot)):
            assert snap.sha256 == digest(snap.document)


def test_listing_fill_then_full_upgrade_preserves_clock_fields(db):
    initial = import_youtube(
        db,
        URL,
        {"id": "AAAAAAAAAAA", "title": "SHEET 5 | 01-16-26", "duration": 36000},
        metadata_kind="listing",
    )
    sparse = import_youtube(
        db, URL, {"id": "AAAAAAAAAAA", "title": "SHEET 5 | 01-16-26"}, metadata_kind="listing"
    )
    assert sparse["duration_seconds"] == 36000
    assert sparse["metadata_completeness"] == "listing"
    assert sparse["metadata_refresh_needed"] is True
    with db.session() as session:
        row = session.get(Source, initial["id"])
        row.document = {**row.document, "event_date": "2026-01-16"}
    full = import_youtube(
        db,
        URL,
        {
            "id": "AAAAAAAAAAA",
            "title": "SHEET 5 | 01-16-26",
            "duration": 36010,
            "formats": [{"format_id": "one", "height": 1080}],
        },
    )
    assert full["metadata_completeness"] == "full"
    assert full["metadata_refresh_needed"] is False
    assert full["duration_seconds"] == 36010
    assert full["event_date"] == "2026-01-16"
    assert full["id"] == initial["id"]


def test_identical_full_refresh_keeps_manifest_hash(db, sources):
    raw = {
        "id": "AAAAAAAAAAA",
        "title": "SHEET 5 | 01-16-26",
        "duration": 36000,
        "upload_date": "20260117",
        "formats": [{"format_id": "1", "height": 1080}],
    }
    repeated = import_youtube(db, URL, raw)
    assert repeated["metadata_sha256"] == sources[0]["metadata_sha256"]
    assert repeated["metadata_refresh_needed"] is False


def test_unchanged_listing_does_not_invalidate_legacy_full_snapshot(db, sources):
    with db.session() as session:
        row = session.get(Source, sources[0]["id"])
        old = dict(row.document)
        old.pop("metadata_completeness", None)
        row.document = old
    listing = import_youtube(
        db,
        URL,
        {"id": "AAAAAAAAAAA", "title": "SHEET 5 | 01-16-26", "duration": 36000},
        metadata_kind="listing",
    )
    assert listing["metadata_completeness"] == "full"
    assert listing["metadata_refresh_needed"] is False
    assert listing["metadata_sha256"] == sources[0]["metadata_sha256"]
