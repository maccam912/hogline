"""Local per-throw replay generation and simultaneous team ghost replay."""

from html import escape
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import FileResponse, HTMLResponse

from hogline.domain.skill_estimates import estimate_report
from hogline.orchestration.throw_reconstructions import (
    FILES,
    ReconstructionRequest,
    artifact_key,
    catalog,
    checked_result,
    enqueue_reconstructions,
    folder,
)


def register_reconstructions(app, db):
    @app.get("/games/{game_id}/reconstructions", response_class=HTMLResponse)
    def viewer(game_id: str):
        estimate_report(db, game_id)
        template = Path(__file__).parent / "static" / "reconstructions.html"
        return HTMLResponse(template.read_text().replace("__GAME_ID__", escape(game_id, quote=True)))

    @app.get("/api/games/{game_id}/reconstructions")
    def reconstruction_catalog(game_id: str):
        return catalog(db, game_id)

    @app.post("/api/games/{game_id}/reconstructions")
    def generate_reconstructions(game_id: str, request: ReconstructionRequest):
        return enqueue_reconstructions(db, game_id, request)

    @app.get("/api/games/{game_id}/reconstructions/{key}/{name}")
    def artifact(game_id: str, key: str, name: str):
        if name not in FILES:
            raise HTTPException(404, "Unknown reconstruction artifact")
        report = estimate_report(db, game_id)
        for player in report["players"]:
            for shot in player["deliveries"]:
                if not shot.get("delivery_ray") or artifact_key(report, shot) != key:
                    continue
                result = checked_result(db, report, shot)
                if (
                    result
                    and result["key"] == key
                    and (name == "manifest.json" or name in result["artifacts"])
                ):
                    root = folder(db, game_id, key)
                    path = root / name
                    if path.resolve().parent != root.resolve():
                        break
                    if name == "source-shot.mp4":
                        from hogline.orchestration.throw_reconstructions import export_asset

                        if export_asset(path) != result["artifacts"][name]:
                            raise HTTPException(404, "Reconstruction clip changed; regenerate it")
                    response = FileResponse(path)
                    response.headers["Cache-Control"] = "no-cache"
                    return response
        raise HTTPException(404, "Reconstruction evidence changed or is unavailable")
