"""Read-only comparisons plus explicit, bounded local measurement requests."""

from fastapi import HTTPException
from fastapi.responses import HTMLResponse, Response

from hogline.analytics.hog_rays import comparison_html, comparison_svg, hog_svg
from hogline.domain.skill_estimates import estimate_report
from hogline.orchestration.hog_rays import measure, saved


def register_hog_rays(app, db):
    def shot_ray(game_id, candidate_id):
        report = estimate_report(db, game_id)
        if report["stale"]:
            raise HTTPException(409, "Refresh the player report before measuring hog rays")
        for player in report["players"]:
            for shot in player["deliveries"]:
                if shot["candidate_id"] == candidate_id and (shot.get("delivery_ray") or {}).get(
                    "perspectives"
                ):
                    return shot["delivery_ray"]
        raise HTTPException(404, "Current perspective delivery ray is unavailable")

    @app.get("/games/{game_id}/hog-rays", response_class=HTMLResponse)
    def page(game_id: str):
        report = estimate_report(db, game_id)
        results = {}
        if not report["stale"]:
            for player in report["players"]:
                for shot in player["deliveries"]:
                    if ray := shot.get("delivery_ray"):
                        results[shot["candidate_id"]] = saved(db, ray)
        return comparison_html(report, results)

    @app.post("/api/games/{game_id}/hog-rays/{candidate_id}")
    def generate(game_id: str, candidate_id: str):
        return measure(db, shot_ray(game_id, candidate_id))

    @app.get("/api/games/{game_id}/hog-rays/{candidate_id}/{view}.svg")
    def image(game_id: str, candidate_id: str, view: str):
        ray = shot_ray(game_id, candidate_id)
        result = saved(db, ray)
        if not result or result["status"] == "unavailable" or view not in ("comparison", "hog"):
            raise HTTPException(404, "Measure a supported hog crossing first")
        svg = comparison_svg(db, ray, result) if view == "comparison" else hog_svg(db, ray, result)
        return Response(svg, media_type="image/svg+xml", headers={"Cache-Control": "no-cache"})
