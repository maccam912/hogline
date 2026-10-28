"""Recording analysis entry points, worker state and immutable reconstruction results."""

from fastapi import HTTPException

from hogline.adapters.sources import digest
from hogline.db import ProcessingBatch
from hogline.orchestration.recording import RecordingAnalysisRequest, start_recording_analysis


def register_recordings(app, db):
    @app.post("/api/recordings/{source_id}/analyze")
    def start_analysis(source_id: str, body: RecordingAnalysisRequest):
        return start_recording_analysis(db, source_id, body)

    @app.get("/api/analyses/{analysis_id}/result")
    def analysis_result(analysis_id: str):
        with db.snapshot_session() as session:
            row = session.get(ProcessingBatch, analysis_id)
            if row is None:
                raise HTTPException(404, "Analysis not found")
            result = row.document.get("analysis_result")
            if result is None:
                raise HTTPException(409, "Results will be available after reconstruction finishes")
            if digest(result) != row.document.get("result_sha256"):
                raise ValueError("Stored recording reconstruction checksum mismatch")
            return result
