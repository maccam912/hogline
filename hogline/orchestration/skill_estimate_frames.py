"""Bounded original-frame acquisition for estimates from already verified local footage."""

from uuid import NAMESPACE_URL, uuid5

from pydantic import Field
from sqlalchemy import select

from hogline.db import Evidence, GameRow
from hogline.domain.catalog import source_or_error
from hogline.domain.models import Model
from hogline.domain.skill_scope import compatible_game_scope
from hogline.media.frames import EvidenceBudget, capture, observe, resolve_media


class EstimateFramesRequest(Model):
    source_id: str
    seconds: list[float] = Field(min_length=1, max_length=128)


def capture_estimate_frames(db, game_id, request):
    times = sorted(set(request.seconds))
    with db.snapshot_session() as session:
        game = session.get(GameRow, game_id)
        if game is None or game.document.get("retired_into"):
            raise ValueError("Choose an active game")
        revision = game.revision
        source = source_or_error(session, request.source_id)
        if not source.get("verified_download") and source.get("platform") != "local":
            raise ValueError("Estimate review uses verified local footage; prepare the recording first")
        if any(
            not any(
                s["source_id"] == request.source_id and s["start_seconds"] <= t <= s["end_seconds"]
                for s in game.document["spans"]
            )
            for t in times
        ):
            raise ValueError("Review timestamps must lie inside this game")
    handle = resolve_media(source)
    budget = EvidenceBudget(db.settings.data_dir, 64, db.settings.max_cache_mb)
    frames = []
    for seconds in times:
        image, timing = capture(handle["uri"], seconds)
        document, _ = observe(image, timing, source, db.settings.data_dir, budget=budget)
        if abs(document["source_seconds"] - seconds) > 0.15:
            raise ValueError("Decoded original timestamp differs from the requested review frame")
        document["id"] = str(uuid5(NAMESPACE_URL, f"estimate-review-frame:{document['evidence_key']}"))
        frames.append(document)
    with db.snapshot_session(write=True) as session:
        if source_or_error(session, request.source_id)["metadata_sha256"] != source[
            "metadata_sha256"
        ] or not compatible_game_scope(session, session.get(GameRow, game_id), revision):
            raise ValueError("Game or recording changed during frame acquisition")
        result = []
        for document in frames:
            row = session.scalar(select(Evidence).where(Evidence.evidence_key == document["evidence_key"]))
            if row is None:
                row = Evidence(
                    id=document["id"],
                    source_id=request.source_id,
                    evidence_key=document["evidence_key"],
                    document=document,
                )
                session.add(row)
            result.append({"id": row.id, "seconds": document["source_seconds"]})
        return {"frames": result, "acquisition": "original_local_video_pts", "paid_requests": 0}
