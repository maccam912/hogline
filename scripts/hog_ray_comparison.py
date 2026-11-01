"""Export a self-contained hog/house-ray comparison; optionally measure missing crossings."""

import argparse
import base64
import re

from hogline.analytics.hog_rays import comparison_html, comparison_svg, hog_svg
from hogline.config import Settings
from hogline.db import Database
from hogline.domain.skill_estimates import estimate_report
from hogline.orchestration.hog_rays import measure, saved


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("game_id")
    parser.add_argument("--measure", action="store_true")
    args = parser.parse_args()
    db = Database(Settings.load())
    report = estimate_report(db, args.game_id)
    if report["stale"]:
        raise ValueError("Refresh the player report before exporting comparisons")
    results, assets = {}, {}
    for player in report["players"]:
        for index, shot in enumerate(player["deliveries"]):
            ray = shot.get("delivery_ray")
            if not ray or not ray.get("perspectives"):
                continue
            if args.measure:
                try:
                    measure(db, ray)
                except ValueError as exc:
                    print(f"{player['name']} {index + 1}: {exc}", flush=True)
            result = saved(db, ray)
            results[shot["candidate_id"]] = result
            if result and result["status"] == "estimated_hog_tangent":
                for view, render in (("comparison", comparison_svg), ("hog", hog_svg)):
                    root = f"/api/games/{args.game_id}/hog-rays/{shot['candidate_id']}/{view}.svg"
                    svg = render(db, ray, result)
                    assets[root] = "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()
    html = comparison_html(report, results)
    for url, data in assets.items():
        html = html.replace(url, data)
    html = re.sub(r"<button\b.*?</button>", "", html)
    html = re.sub(r"<script\b.*?</script>", "", html)
    html = html.replace('href="/api/', 'href="http://127.0.0.1:8765/api/')
    root = db.settings.data_dir / "hog-rays"
    root.mkdir(exist_ok=True)
    path = root / "comparison.html"
    path.write_text(html)
    print(f"Saved {len(assets) // 2} comparisons to {path}")


if __name__ == "__main__":
    main()
