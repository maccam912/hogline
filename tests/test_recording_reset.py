from fastapi.testclient import TestClient
from sqlalchemy import select

from hogline.db import Evidence, FrameObservation, Job, Scan, Source
from hogline.review.app import create_app


def seed(db, source_id, suffix):
    with db.session() as session:
        job = Job(
            id="job-" + suffix,
            job_key="key-" + suffix,
            state="succeeded",
            document={"kind": "scan", "request": {"source_id": source_id}},
        )
        session.add(job)
        session.flush()
        session.add(Scan(id="scan-" + suffix, job_id=job.id, source_id=source_id, document={}))
        frame = Evidence(
            id="frame-" + suffix,
            source_id=source_id,
            evidence_key="frame-" + suffix,
            document={"source_id": source_id},
        )
        session.add(frame)
        session.flush()
        session.add(
            FrameObservation(id="obs-" + suffix, evidence_id=frame.id, model_version="test", document={})
        )
        session.add(
            FrameObservation(
                id="review-" + suffix,
                evidence_id=frame.id,
                model_version="review",
                document={"kind": "independent-visual-ice-end-v1"},
            )
        )


def test_reset_is_scoped_and_keeps_frames_and_independent_reviews(db, sources):
    seed(db, sources[0]["id"], "one")
    seed(db, sources[1]["id"], "two")
    with TestClient(create_app(db.settings)) as client:
        path = "/api/recordings/" + sources[0]["id"]
        preview = client.get(path + "/reset-preview").json()
        assert preview["blockers"] == []
        result = client.post(path + "/reset", json={"expected_sha256": preview["sha256"]})
        assert result.status_code == 200, result.text
    with db.session() as session:
        assert session.get(Job, "job-one") is None
        assert session.get(Scan, "scan-one") is None
        assert session.get(FrameObservation, "obs-one") is None
        assert session.get(FrameObservation, "review-one") is not None
        assert session.get(Evidence, "frame-one") is not None
        assert session.get(Source, sources[0]["id"]) is not None
        assert session.get(Job, "job-two") is not None


def test_reset_rejects_stale_preview_and_live_jobs(db, sources):
    seed(db, sources[0]["id"], "one")
    with TestClient(create_app(db.settings)) as client:
        path = "/api/recordings/" + sources[0]["id"]
        preview = client.get(path + "/reset-preview").json()
        with db.session() as session:
            session.get(Job, "job-one").state = "running"
        assert client.post(path + "/reset", json={"expected_sha256": preview["sha256"]}).status_code == 409
        latest = client.get(path + "/reset-preview").json()
        assert latest["blockers"]
        assert client.post(path + "/reset", json={"expected_sha256": latest["sha256"]}).status_code == 422
        with db.session() as session:
            assert len(list(session.scalars(select(Scan)))) == 1
