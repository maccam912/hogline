from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import event

from hogline.db import AnalysisWindow, Evidence, FrameObservation, Job, ProcessingBatch, WindowTracks
from hogline.media.frames import observe
from hogline.orchestration.recording import RecordingAnalysisRequest, start_recording_analysis
from hogline.perception.stones import VERSION
from hogline.review.app import create_app


def test_bootstrap_keeps_inventory_and_live_jobs_without_loading_historical_payloads(db, sources):
    batches = [start_recording_analysis(db, s["id"], RecordingAnalysisRequest()) for s in sources]
    with db.session() as session:
        batch = session.get(ProcessingBatch, batches[0]["id"])
        batch.document = {**batch.document, "analysis_result": {"large_saved_report": "x" * 1_000_000}}
        job = session.get(Job, batches[0]["units"][0]["job_id"])
        job.state = "running"
        session.add(Job(job_key="historical", state="succeeded", document={"old_payload": "x" * 100_000}))
    app = create_app(db.settings)
    statements = []

    def record(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    event.listen(app.state.db.engine, "before_cursor_execute", record)
    with TestClient(app) as client:
        response = client.get("/api/workspace")
        data = response.json()
        assert {s["id"] for s in data["sources"]} == {s["id"] for s in sources}
        assert len(data["batches"]) == 2 and all(b["summary_only"] for b in data["batches"])
        assert data["jobs"][0]["state"] == "running" and len(data["jobs"]) == 1
        assert data["evidence"] == data["scans"] == data["analysis_windows"] == []
        assert "large_saved_report" not in response.text and "old_payload" not in response.text
        assert len(response.content) < 50_000
        assert not any("FROM frame_observation" in s or "FROM evidence_frame" in s for s in statements)
        batch_queries = [s for s in statements if "FROM processing_batch" in s]
        assert batch_queries and "JSON_EXTRACT(processing_batch.document" in batch_queries[0]
        assert "processing_batch.state, processing_batch.document" not in batch_queries[0]
        assert (
            client.get("/api/workspace", headers={"If-None-Match": response.headers["etag"]}).status_code
            == 304
        )


def test_source_workspace_isolated_from_other_recording_data_and_corrupt_analysis(db, sources):
    batches = [start_recording_analysis(db, s["id"], RecordingAnalysisRequest()) for s in sources]
    ids = []
    with db.session() as session:
        for i, source in enumerate(sources):
            doc, _ = observe(
                Image.new("RGB", (640, 360)),
                {"pts": i, "time_base_num": 1, "time_base_den": 1},
                source,
                db.settings.data_dir,
            )
            frame = Evidence(source_id=source["id"], evidence_key=doc["evidence_key"], document=doc)
            session.add(frame)
            session.flush()
            ids.append(frame.id)
            session.add(
                FrameObservation(
                    id=f"observation-{i}",
                    evidence_id=frame.id,
                    model_version=VERSION,
                    document={"id": f"observation-{i}", "evidence_id": frame.id, "views": {}},
                )
            )
        window = AnalysisWindow(
            id="other-recording-window",
            source_id=sources[1]["id"],
            job_id=batches[1]["units"][0]["job_id"],
            document={},
        )
        session.add(window)
        session.flush()
        session.add(
            WindowTracks(
                id="corrupt-other-recording",
                window_id=window.id,
                version="test",
                sha256="wrong",
                document={},
            )
        )
    with TestClient(create_app(db.settings)) as client:
        overview = client.get("/api/workspace")
        assert overview.status_code == 200
        response = client.get(f"/api/sources/{sources[0]['id']}/workspace")
        assert response.status_code == 200
        data = response.json()
        assert [e["id"] for e in data["evidence"]] == ids[:1]
        assert [o["evidence_id"] for o in data["observations"]] == ids[:1]
        assert [b["id"] for b in data["batches"]] == [batches[0]["id"]]
        assert all(j["batch_id"] == batches[0]["id"] for j in data["jobs"])
        assert data["batches"][0]["progress_summary"]["overall_fraction"] == 0
        assert client.get(f"/api/sources/{sources[1]['id']}/workspace").status_code == 422
        assert client.get("/api/sources/missing/workspace").status_code == 404
        assert client.get("/api/workspace").status_code == 200
        assert (
            client.get(
                f"/api/sources/{sources[0]['id']}/workspace",
                headers={"If-None-Match": response.headers["etag"]},
            ).status_code
            == 304
        )


def test_source_workspace_caches_do_not_cross_recordings_and_mutations_invalidate(db, sources):
    with TestClient(create_app(db.settings)) as client:
        a = client.get(f"/api/sources/{sources[0]['id']}/workspace")
        b = client.get(
            f"/api/sources/{sources[1]['id']}/workspace",
            headers={"If-None-Match": a.headers["etag"]},
        )
        assert b.status_code == 200 and b.json()["source_id"] == sources[1]["id"]
        assert client.get("/api/sources/missing/boundary-links").status_code == 404
        assert client.get(f"/api/sources/{sources[0]['id']}/boundary-links").json()["links"] == []
        response = client.post("/api/scans", json={"source_id": sources[0]["id"], "end_seconds": 50})
        assert response.status_code == 200
        overview = client.get("/api/workspace").json()
        assert overview["jobs"][0]["id"] == response.json()["id"]
