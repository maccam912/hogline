import copy

import pytest
from PIL import Image, ImageDraw, ImageFont
from sqlalchemy import select

from hogline.adapters.sources import digest
from hogline.analytics.exports import snapshot
from hogline.analytics.lineage import validate_references
from hogline.analytics.scorecards import scorecard_observations_in_session
from hogline.config import profile
from hogline.db import FrameObservation, Job, ProcessingBatch
from hogline.domain.models import ScanRequest
from hogline.orchestration import scorecards as jobs
from hogline.orchestration import worker
from hogline.perception.scorecard import fixed_header_cells, read_middle_row
from tests.test_worker import fake_capture


class Reader:
    available = True

    def __init__(self, values=(), **_kwargs):
        self.values = iter(values)

    def read(self, *_args, **_kwargs):
        return {"text": next(self.values, ""), "engine_confidence": 90}


def board_image(*, blank=True, reverse=False, current=True):
    image = Image.new("RGB", (800, 150), (35, 50, 100))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 799, 44), fill=(180, 30, 30))
    draw.rectangle((0, 105, 799, 149), fill=(230, 210, 40))
    centers = [0.12 + i * 0.06 for i in range(15)]
    font = ImageFont.load_default(size=28)
    for index, center in enumerate(centers):
        draw.text((round(center * 800) - 8, 53), str(index + 1), fill="white", font=font)
    if current:
        draw.rectangle((28, 48, 55, 100), fill="white")
        draw.text((33, 54), "7", fill="black", font=font)
    if blank:
        x = round(centers[2] * 800)
        draw.rectangle((x - 17, 48, x + 17, 100), fill="white")
        if not reverse:
            draw.text((x - 8, 54), "3", fill="black", font=font)
    template = {
        "bounds": [0, 0, 1, 1],
        "rows": {"red": [0, 0.3], "yellow": [0.7, 1]},
        "point_centers": centers,
    }
    return image, template


def test_fixed_white_header_numbers_are_not_blank_plates():
    image, template = board_image(blank=False, current=False)
    result = read_middle_row(image, template, Reader())
    assert result["current_end"] is None
    assert result["blank_end_cards"] == []
    assert result["unassigned_blank_plates"] == []


def test_current_end_and_explicit_matching_middle_plate_have_different_meanings():
    image, template = board_image()
    result = read_middle_row(image, template, Reader(["7", "7", "3", "3"]))
    assert result["current_end"]["number"] == 7
    assert result["blank_end_cards"][0]["end_number"] == 3
    assert len(result["blank_end_cards"]) == 1


def test_reversed_blank_is_not_given_an_invented_end_number():
    image, template = board_image(reverse=True, current=False)
    result = read_middle_row(image, template, Reader())
    assert result["blank_end_cards"] == []
    assert result["unassigned_blank_plates"][0]["status"] == "reversed_blank_plate_candidate"
    assert result["unassigned_blank_plates"][0]["end_number"] is None


def test_low_chroma_printed_eight_is_rejected_against_fixed_header_reference():
    image, template = board_image(blank=False, current=False)
    template["layout_reference"] = {"header_bounds": [0, 0.3, 1, 0.7]}
    references = fixed_header_cells(image, template)
    import numpy as np

    pixels = np.asarray(image).copy()
    pixels[(pixels[:, :, 2] == 100) & (pixels[:, :, 0] == 35)] = (65, 70, 88)
    result = read_middle_row(Image.fromarray(pixels), template, Reader(["8"] * 32), references)
    assert result["blank_end_cards"] == []
    assert any(
        c["status"] == "fixed_printed_number; not_blank_plate" for c in result["middle_slot_visibility"]
    )


@pytest.fixture
def cached_boards(db, sources, monkeypatch):
    monkeypatch.setattr(worker, "resolve_media", lambda *_: {"uri": "fixture", "format_id": "fixture"})
    monkeypatch.setattr(worker, "capture", fake_capture)
    worker.enqueue(
        db, ScanRequest(source_id=sources[0]["id"], start_seconds=0, end_seconds=60, interval_seconds=20)
    )
    assert worker.run_one(db)["state"] == "succeeded"
    layout = profile()
    _, template = board_image()
    layout["broadcast"]["scoreboards"]["5"] = template
    monkeypatch.setattr(jobs, "profile", lambda: layout)
    monkeypatch.setattr(jobs, "TesseractOCR", Reader)
    parents = []
    from hogline.db import Evidence

    with db.session() as session:
        for frame in session.scalars(select(Evidence)):
            document = {
                "id": f"parent-{frame.id}",
                "evidence_id": frame.id,
                "source_id": frame.source_id,
                "sheet": 5,
                "source_seconds": frame.document["source_seconds"],
                "model_version": "fixture-board",
                "scoreboard": {"status": "observed", "effective_bounds": [0, 0, 1, 1], "cards": []},
            }
            parents.append(document)
            session.add(
                FrameObservation(
                    id=document["id"], evidence_id=frame.id, model_version="fixture-board", document=document
                )
            )
    monkeypatch.setattr(jobs, "broadcast_catalog_in_session", lambda *_: {"observations": parents})
    monkeypatch.setattr(
        jobs,
        "read_frame",
        lambda *_: {
            "status": "observed",
            "cards": [{"color": "red", "end_number": 1, "cumulative_points": 2}],
            "current_end": {"number": 2},
            "blank_end_cards": [],
            "unreadable_slots": [],
        },
    )
    return sources[0], parents


def test_durable_reader_is_idempotent_and_exported_with_checked_parents(db, cached_boards):
    source, _parents = cached_boards
    batch = jobs.create_scorecard_batch(db, source["id"])
    assert jobs.create_scorecard_batch(db, source["id"])["id"] == batch["id"]
    assert worker.run_one(db)["state"] == "succeeded"
    with db.session() as session:
        observations = scorecard_observations_in_session(session)
        assert len(observations) == 3
        assert all(o["ice_views"] for o in observations)
        assert all(digest({k: v for k, v in o.items() if k != "sha256"}) == o["sha256"] for o in observations)
    reused = jobs.create_scorecard_batch(db, source["id"])
    assert reused["state"] == "completed"
    assert worker.run_one(db) is None
    release = snapshot(db, "exploratory")
    assert len(release["scorecard_observations"]) == 3
    validate_references(release)
    bad = copy.deepcopy(release)
    bad["scorecard_parent_observations"][0]["evidence_id"] = "different"
    with pytest.raises(ValueError, match="parent observation"):
        validate_references(bad)


def test_reader_rejects_changed_parent_before_publishing(db, cached_boards):
    source, parents = cached_boards
    jobs.create_scorecard_batch(db, source["id"])
    with db.session() as session:
        row = session.get(FrameObservation, parents[0]["id"])
        row.document = {**row.document, "changed": True}
    result = worker.run_one(db)
    assert result["state"] == "failed" and "parent" in result["error"]
    with db.session() as session:
        assert not scorecard_observations_in_session(session)
        assert any(j.state == "failed" for j in session.scalars(select(Job)))


def test_partial_replacement_does_not_erase_completed_reader_history(db, cached_boards):
    source, _parents = cached_boards
    batch = jobs.create_scorecard_batch(db, source["id"])
    assert worker.run_one(db)["state"] == "succeeded"
    with db.session() as session:
        current = scorecard_observations_in_session(session)
        old_version = "wausau-middle-row-plates-v2:previous"
        for doc in current:
            previous = {**copy.deepcopy(doc), "id": f"previous-{doc['id']}", "model_version": old_version}
            previous["sha256"] = digest({k: v for k, v in previous.items() if k != "sha256"})
            session.add(
                FrameObservation(
                    id=previous["id"],
                    evidence_id=previous["evidence_id"],
                    model_version=old_version,
                    document=previous,
                )
            )
        session.add(
            ProcessingBatch(
                id="previous-pass",
                state="completed",
                document={
                    "kind": "scorecard_read",
                    "units": [{"payload": {"model_version": old_version}}],
                },
            )
        )
        session.get(ProcessingBatch, batch["id"]).state = "active"
    with db.session() as session:
        assert {o["model_version"] for o in scorecard_observations_in_session(session)} == {old_version}
        session.get(ProcessingBatch, batch["id"]).state = "completed"
    with db.session() as session:
        assert {o["model_version"] for o in scorecard_observations_in_session(session)} == {
            current[0]["model_version"]
        }
