from uuid import NAMESPACE_URL, uuid5

import pytest
from sqlalchemy import func, select

from hogline.config import profile
from hogline.db import Evidence, FrameObservation
from hogline.perception import broadcast
from hogline.perception.ocr import OCRBudgetReached, TesseractOCR


def add_frames(db, source, count, *, coarse=False, processed=False, start=0):
    ids = []
    with db.session() as session:
        for i in range(start, start + count):
            frame = Evidence(
                source_id=source["id"],
                evidence_key=f"{source['id']}:{i}",
                document={"source_seconds": i, **({"acquisition_tier": "coarse"} if coarse else {})},
            )
            session.add(frame)
            session.flush()
            ids.append(frame.id)
            if processed:
                fake_read(session, frame, None, profile(), None, None)
    return ids


def fake_read(session, frame, root, layout, reader, budget):
    version = broadcast.broadcast_version(layout)
    identifier = str(uuid5(NAMESPACE_URL, f"{frame.id}:{version}"))
    doc = {"id": identifier, "evidence_id": frame.id, "model_version": version}
    session.add(FrameObservation(id=identifier, evidence_id=frame.id, model_version=version, document=doc))
    return doc


@pytest.fixture
def fake_ocr(monkeypatch):
    monkeypatch.setattr(TesseractOCR, "available", property(lambda self: True))
    monkeypatch.setattr(broadcast, "analyze_broadcast_frame", fake_read)


def test_batches_skip_completed_and_coarse_before_limiting_and_resume(db, sources, fake_ocr):
    add_frames(db, sources[0], 130, processed=True)
    add_frames(db, sources[0], 130, coarse=True, start=130)
    pending = add_frames(db, sources[0], 5, start=260)
    first = broadcast.analyze_broadcast(db, limit=2)
    assert [x["evidence_id"] for x in first["observations"]] == pending[:2]
    assert first["remaining_frames"] == 3 and first["stopped_reason"] == "frame_limit"
    second = broadcast.analyze_broadcast(db, limit=2)
    assert [x["evidence_id"] for x in second["observations"]] == pending[2:4]
    last = broadcast.analyze_broadcast(db, limit=2)
    assert last["status"] == "complete" and last["remaining_frames"] == 0
    assert last["observations"][0]["evidence_id"] == pending[-1]
    assert broadcast.analyze_broadcast(db)["observations"] == []


def test_scope_and_model_versions_remain_independent(db, sources, fake_ocr, monkeypatch):
    first = add_frames(db, sources[0], 2, processed=True)
    second = add_frames(db, sources[1], 2)
    result = broadcast.analyze_broadcast(db, first)
    assert result["processed_frames"] == result["remaining_frames"] == 0
    monkeypatch.setattr(broadcast, "VERSION", "new-test-ocr-version")
    result = broadcast.analyze_broadcast(db, first)
    assert {o["evidence_id"] for o in result["observations"]} == set(first)
    assert broadcast.analyze_broadcast(db, second)["processed_frames"] == 2


def test_deadline_rolls_back_partial_frame_but_preserves_completed_frames(db, sources, fake_ocr, monkeypatch):
    pending = add_frames(db, sources[0], 3)
    calls = []

    def interrupted(*args):
        result = fake_read(*args)
        calls.append(result["evidence_id"])
        if len(calls) == 2:
            raise OCRBudgetReached("time budget")
        return result

    monkeypatch.setattr(broadcast, "analyze_broadcast_frame", interrupted)
    first = broadcast.analyze_broadcast(db)
    assert first["stopped_reason"] == "worker_time_budget"
    assert first["processed_frames"] == 1 and first["remaining_frames"] == 2
    with db.session() as session:
        assert session.scalar(select(func.count()).select_from(FrameObservation)) == 1
    monkeypatch.setattr(broadcast, "analyze_broadcast_frame", fake_read)
    resumed = broadcast.analyze_broadcast(db)
    assert [x["evidence_id"] for x in resumed["observations"]] == pending[1:]
    assert resumed["status"] == "complete"


@pytest.mark.parametrize(
    "kwargs", [{"limit": 0}, {"limit": 121}, {"max_seconds": float("nan")}, {"max_seconds": 121}]
)
def test_invalid_budgets_fail_before_processing(db, kwargs):
    with pytest.raises(ValueError):
        broadcast.analyze_broadcast(db, **kwargs)


def test_ocr_subprocess_timeout_uses_remaining_pass_budget(monkeypatch):
    import subprocess

    from PIL import Image

    from hogline.perception import ocr

    clock = iter([10, 10, 12])
    monkeypatch.setattr(ocr.time, "monotonic", lambda: next(clock))
    timeouts = []

    def timeout(command, **kwargs):
        timeouts.append(kwargs["timeout"])
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(ocr.subprocess, "run", timeout)
    with pytest.raises(OCRBudgetReached):
        TesseractOCR(deadline=11).read(Image.new("RGB", (20, 20), "white"))
    assert timeouts == [1]
