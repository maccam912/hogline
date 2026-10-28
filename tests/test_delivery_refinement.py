import copy

import pytest
from sqlalchemy import select

from hogline.adapters.sources import digest
from hogline.db import AnalysisWindow, WindowActors, WindowDeliveries
from hogline.orchestration import worker
from hogline.orchestration.automatic import AutomaticRequest, create_automatic_batch
from hogline.orchestration.delivery_refinement import refinement_units
from tests.test_actors import fake_people, fake_window  # noqa: F401
from tests.test_automatic import batch, drain, media  # noqa: F401
from tests.test_players import player_result  # noqa: F401


def seed_brackets(session, window_id):
    row = session.scalar(select(WindowDeliveries).where(WindowDeliveries.window_id == window_id))
    doc = copy.deepcopy(row.document)
    # Synthetic planner inputs: these hypotheses test selection, not physical release accuracy.
    doc["candidates"] = [{"earliest_release_seconds": 101.5, "latest_release_seconds": 102}]
    doc["uncertain_motion"] = [
        {
            "reasons": ["no_observed_stationary_control_prelude"],
            "control_intervals": [
                {"sample_count": 3, "first": {"source_seconds": 101}, "last": {"source_seconds": 102}}
            ],
        }
    ]
    row.document, row.sha256 = doc, digest(doc)
    return row


def request(sources, **changes):
    return AutomaticRequest(
        source_ids=[sources[0]["id"]],
        start_seconds=100,
        end_seconds=110,
        scan_jobs=1,
        detail_jobs=1,
        ocr_frames=1,
        **changes,
    )


def test_refinements_are_scoped_deduplicated_bounded_and_lineage_pinned(db, sources, player_result):  # noqa: F811
    with db.snapshot_session(write=True) as session:
        row = seed_brackets(session, player_result["window_id"])
        units, summary = refinement_units(
            session, request(sources), {sources[0]["id"]: sources[0]["metadata_sha256"]}, [row.window_id]
        )
        assert len(units) == 1
        unit = units[0]
        q = unit["payload"]["request"]
        assert (q["start_seconds"], q["end_seconds"], q["interval_seconds"]) == (100, 104, 1 / 30)
        assert q["max_frames"] == 121 and q["max_worker_seconds"] == 120
        assert unit["selection"]["delivery_result_sha256"] == row.sha256
        assert unit["selection"]["reason"] == "release_bracket"
        assert summary["diagnostics"]["overlapping_refinement_excluded"] == 1
        assert not refinement_units(
            session, request(sources), {sources[0]["id"]: sources[0]["metadata_sha256"]}, []
        )[0]
        assert not refinement_units(
            session,
            request(sources, delivery_refinement_jobs=0),
            {sources[0]["id"]: sources[0]["metadata_sha256"]},
            [row.window_id],
        )[0]
        assert not refinement_units(
            session, request(sources), {sources[0]["id"]: "earlier"}, [row.window_id]
        )[0]
        row.document = {**row.document, "notice": "Corrupt"}
    with pytest.raises(ValueError, match="checksum"):
        with db.snapshot_session() as session:
            refinement_units(
                session,
                request(sources),
                {sources[0]["id"]: sources[0]["metadata_sha256"]},
                [player_result["window_id"]],
            )


def test_refinement_abstains_without_control_and_refuses_out_of_window_brackets(db, sources, player_result):  # noqa: F811
    with db.snapshot_session(write=True) as session:
        row = seed_brackets(session, player_result["window_id"])
        doc = copy.deepcopy(row.document)
        doc["candidates"] = []
        doc["uncertain_motion"] = [
            {"reasons": ["no_observed_persistent_hand_control"], "control_intervals": []}
        ]
        row.document, row.sha256 = doc, digest(doc)
        assert not refinement_units(
            session, request(sources), {sources[0]["id"]: sources[0]["metadata_sha256"]}, [row.window_id]
        )[0]
        doc = copy.deepcopy(doc)
        doc["candidates"] = [{"earliest_release_seconds": 0, "latest_release_seconds": 1}]
        row.document, row.sha256 = doc, digest(doc)
    with pytest.raises(ValueError, match="outside its original"):
        with db.snapshot_session() as session:
            refinement_units(
                session, request(sources), {sources[0]["id"]: sources[0]["metadata_sha256"]}, [row.window_id]
            )


@pytest.mark.usefixtures("media", "fake_people")
def test_campaign_automatically_refines_once_then_finishes_ocr_without_expanding_budget(db, sources):
    result = create_automatic_batch(db, request(sources))
    assert worker.run_one(db)["state"] == "succeeded"  # Sparse scan.
    assert worker.run_one(db)["state"] == "succeeded"  # Ordinary detail.
    with db.snapshot_session(write=True) as session:
        window = session.scalar(select(AnalysisWindow))
        assert session.scalar(select(WindowActors)).document["tracks"]
        seed_brackets(session, window.id)
    assert worker.run_one(db)["state"] == "succeeded"  # Automatically selected native-cadence detail.
    doc = batch(db, result["id"])
    assert doc["phase"] == "delivery_detail"
    refinements = [u for u in doc["units"] if u.get("selection", {}).get("reason") == "release_bracket"]
    assert len(refinements) == 1 and refinements[0]["payload"]["request"]["interval_seconds"] == 1 / 30
    drain(db)
    completed = batch(db, result["id"])
    assert completed["state"] == "completed"
    assert [p["phase"] for p in completed["phase_history"]] == ["detail", "delivery_detail", "ocr"]
    assert completed["request"]["max_worker_seconds"] == 600
    assert completed["spent_worker_seconds"] < 600 and completed["reserved_image_bytes"] < 128 * 1024**2
    assert worker.run_one(db) is None
