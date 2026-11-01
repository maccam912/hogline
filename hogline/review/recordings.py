"""Recording analysis entry points, worker state and immutable reconstruction results."""

from fastapi import HTTPException

from hogline.adapters.sources import digest
from hogline.db import ProcessingBatch
from hogline.orchestration.recording import RecordingAnalysisRequest, start_recording_analysis


def register_recordings(app, db):
    from hogline.review.reset import register_reset

    register_reset(app, db)

    @app.get("/api/recordings/{source_id}/status")
    def status(source_id: str):
        from hogline.review.recording_status import recording_status

        return recording_status(db, source_id)

    @app.post("/api/recordings/{source_id}/analyze")
    def start_analysis(source_id: str, body: RecordingAnalysisRequest):
        from hogline.domain.catalog import source_or_error
        from hogline.media.post_live import has_verified_download, prepare_download

        with db.snapshot_session() as session:
            source = dict(source_or_error(session, source_id))
        if source.get("live_status") == "post_live" and not has_verified_download(source):
            prepare_download(db, source_id)
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
