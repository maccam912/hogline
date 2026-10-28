import copy

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from hogline.analytics.moments import MomentQuery, build_moments, search_index
from hogline.domain.catalog import Conflict
from hogline.perception.scoreboard import persistent_board_claims
from hogline.reconstruction.board_events import board_timeline
from hogline.review.app import create_app
from tests.test_board_events import run


def fixture():
    observations = run(10) + run(30, red=((1, 2), (3, 4)))
    data = {
        "sources": [
            {
                "id": "source-1",
                "title": "SHEET 1 | 03-17-26",
                "sheet": 1,
                "title_date": "2026-03-17",
                "metadata_sha256": "manifest-1",
                "platform": "youtube",
                "platform_id": "AAAAAAAAAAA",
            }
        ],
        "evidence": [
            {"id": o["evidence_id"], "source_id": o["source_id"], "source_seconds": o["source_seconds"]}
            for o in observations
        ],
        "analysis_windows": [],
        "games": [],
    }
    broadcast = {
        "board_timeline": board_timeline(observations),
        "score_proposals": persistent_board_claims(observations),
    }
    return data, broadcast


def test_moments_keep_brackets_proposals_and_exact_source_links():
    data, broadcast = fixture()
    moments = build_moments(data, broadcast, [])
    result = search_index(data, moments, MomentQuery())
    assert result["total"] == 2
    change = next(m for m in result["moments"] if m["kind"] == "board_change")
    assert (change["start_seconds"], change["end_seconds"]) == (20, 30)
    assert change["start_youtube_url"].endswith("&t=20s")
    assert change["end_youtube_url"].endswith("&t=30s")
    assert change["source_metadata_sha256"] == "manifest-1"
    assert len(change["evidence"]) == 6
    assert all(m["acceptance"] == "proposed" for m in moments)
    assert search_index(data, moments, MomentQuery(acceptance="human_confirmed"))["total"] == 0
    assert search_index(data, moments, MomentQuery(text="RED end 3"))["total"] == 1
    assert search_index(data, moments, MomentQuery(sheet=8))["total"] == 0
    assert search_index(data, moments, MomentQuery(title_date_from="2026-03-18"))["total"] == 0
    assert search_index(data, moments, MomentQuery(kinds=["board_change"]))["total"] == 1
    with pytest.raises(ValidationError):
        MomentQuery(title_date_from="2026-04-01", title_date_to="2026-03-01")


def test_pagination_detects_new_evidence_and_stale_inputs_are_explicit():
    data, broadcast = fixture()
    moments = build_moments(data, broadcast, [])
    first = search_index(data, moments, MomentQuery(limit=1))
    assert first["next_offset"] == 1
    second = search_index(
        data, moments, MomentQuery(offset=1, limit=1, expected_index_sha256=first["index_sha256"])
    )
    assert second["next_offset"] is None
    assert first["moments"][0]["id"] != second["moments"][0]["id"]
    with pytest.raises(Conflict, match="index changed"):
        search_index(data, moments[:1], MomentQuery(expected_index_sha256=first["index_sha256"]))
    data["sources"][0]["metadata_sha256"] = "refreshed"
    stale = build_moments(data, broadcast, [])
    assert search_index(data, stale, MomentQuery())["total"] == 0
    assert search_index(data, stale, MomentQuery(include_earlier_inputs=True))["total"] == 2
    with pytest.raises(ValueError, match="Recording not found"):
        search_index(data, stale, MomentQuery(source_id="missing"))


def test_saved_end_points_to_its_evidence_and_retired_games_are_excluded():
    data, broadcast = fixture()
    data["games"] = [
        {
            "id": "game",
            "revision": 4,
            "label": "Reviewed game",
            "ends": [
                {
                    "id": "end",
                    "official_number": 2,
                    "observed_index": 1,
                    "state": "played",
                    "red_points": 2,
                    "yellow_points": 0,
                    "acceptance": "human_confirmed",
                    "evidence_ids": ["frame-10", "frame-20"],
                }
            ],
        }
    ]
    result = search_index(data, build_moments(data, broadcast, []), MomentQuery(acceptance="human_confirmed"))
    assert result["total"] == 1
    moment = result["moments"][0]
    assert moment["start_seconds"] == 10 and moment["end_seconds"] == 20
    assert moment["reference"]["game_revision"] == 4
    retired = copy.deepcopy(data)
    retired["games"][0]["retired_into"] = ["successor"]
    assert not search_index(
        retired, build_moments(retired, broadcast, []), MomentQuery(acceptance="human_confirmed")
    )["moments"]


def test_empty_archive_search_and_request_validation(db, sources):
    client = TestClient(create_app(db.settings))
    result = client.post("/api/moments/search", json={}).json()
    assert result["total"] == 0 and result["coverage"]["inventoried_recordings"] == 2
    assert client.post("/api/moments/search", json={"limit": 101}).status_code == 422
    assert client.post("/api/moments/search", json={"kinds": ["invented_shot"]}).status_code == 422
    assert client.post("/api/moments/search", json={"source_id": "missing"}).status_code == 422
    assert client.post("/api/moments/search", json={"expected_index_sha256": "f" * 64}).status_code == 409


def test_motion_search_excludes_stationary_fragments_and_old_methods_by_default():
    from hogline.adapters.sources import digest
    from hogline.config import profile
    from hogline.perception.stones import VERSION as detector
    from hogline.perception.tracking import VERSION as tracker
    from hogline.perception.visibility import VERSION as visibility

    data, _ = fixture()
    track = {
        "id": "moving",
        "motion_class": "position_change_observed",
        "start_seconds": 12.5,
        "end_seconds": 15.5,
        "color": "yellow",
        "net_displacement_px": 18,
        "view": "house_top",
    }
    result = {
        "id": "result",
        "window_id": "window",
        "source_id": "source-1",
        "source_metadata_sha256": "manifest-1",
        "version": tracker,
        "detector_version": detector,
        "visibility_version": visibility,
        "profile_sha256": digest(profile()),
        "context_anchor_evidence_ids": ["frame-10", "frame-20"],
        "tracks": [track, {**track, "id": "stationary", "motion_class": "small_net_displacement"}],
    }
    moments = build_moments(data, {}, [result])
    assert len(moments) == 1
    assert moments[0]["start_seconds"] == 12.5
    assert moments[0]["start_youtube_url"].endswith("&t=12s")
    assert [e["source_seconds"] for e in moments[0]["evidence"]] == [10, 20]
    assert moments[0]["reference"]["track_id"] == "moving"
    result["version"] = "old-tracker"
    old = build_moments(data, {}, [result])
    assert search_index(data, old, MomentQuery())["total"] == 0
    assert search_index(data, old, MomentQuery(include_earlier_inputs=True))["total"] == 1
