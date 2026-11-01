"""Compare the original delivery-house ray with a local delivery-hog tangent."""

import base64
from html import escape

from hogline.analytics.delivery_rays import ray_svg
from hogline.orchestration.hog_rays import key
from hogline.perception.delivery_ray import image_points
from hogline.perception.perspective_rays import HOG_FROM_TEE_M, project
from hogline.perception.throw_reconstruction import orientation


def comparison_svg(db, ray, result):
    svg = ray_svg(db, ray, "receiving")
    sign = orientation(ray)
    target = result["target_tee_x_m"] * sign
    slope = result["slope_transverse_per_forward"] * sign
    q = image_points(
        [[target + slope * ray["forward_sign"] * y, y] for y in [-2.2, 2.2]], ray["receiving_geometry"]
    )
    coords = " ".join(f"{x:.2f},{y:.2f}" for x, y in q)
    dot = image_points([[target, 0]], ray["receiving_geometry"])[0]
    overlay = f'<polyline points="{coords}" fill="none" stroke="white" stroke-width="5"/>'
    overlay += (
        f'<polyline points="{coords}" fill="none" stroke="#009b56" '
        'stroke-width="2.5" stroke-dasharray="7 3"/>'
    )
    overlay += f'<circle cx="{dot[0]:.2f}" cy="{dot[1]:.2f}" r="4" fill="#009b56" stroke="white"/>'
    # When the tee crossing lies outside the frame, keep a visible edge marker.
    if abs(target) > 1.9:
        overlay += (
            '<text x="8" y="305" font-size="11" fill="#006b3b" '
            f'stroke="white" stroke-width=".2">Hog tee crossing {target:+.2f} m</text>'
        )
    return svg.replace("</g>", overlay + "</g>", 1)


def hog_svg(db, ray, result):
    path = db.settings.data_dir / "hog-rays" / (key(ray) + ".png")
    camera = ray["perspective_calibration"]["cameras"][result["camera"]]
    # The visible hog line belongs to the calibrated original camera landmarks.
    hog = project([[-2.375, HOG_FROM_TEE_M], [2.375, HOG_FROM_TEE_M]], camera["local_to_pixel"])
    coords = " ".join(f"{x:.2f},{y:.2f}" for x, y in hog)
    encoded = base64.b64encode(path.read_bytes()).decode()
    dots = "".join(
        f'<circle cx="{p["pixel"][0]:.2f}" cy="{p["pixel"][1]:.2f}" r="1.5" fill="#00ee88"/>'
        for p in result["points"]
    )
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 290 480 190">'
        "<title>Native hog-line observations</title>"
        f'<image width="480" height="684" href="data:image/png;base64,{encoded}"/>'
        f'<polyline points="{coords}" stroke="#ffb400" stroke-width="1" stroke-dasharray="4 3"/>{dots}</svg>'
    )


def comparison_html(report, results):
    game = escape(report["game_id"], quote=True)
    sections = []
    for player in report["players"]:
        shots = []
        for index, shot in enumerate(player["deliveries"]):
            cid = escape(shot["candidate_id"], quote=True)
            result = results.get(shot["candidate_id"])
            root = f"/api/games/{game}/hog-rays/{cid}"
            content = "<p>Not measured yet.</p>"
            ready = result and result["status"] == "estimated_hog_tangent"
            if result and not ready:
                content = f"<p>Unavailable: {escape(result['error'])}.</p>"
            if ready:
                sign = orientation(shot["delivery_ray"])
                old = shot["delivery_ray"]["target_tee_x_m"] * sign
                new = result["target_tee_x_m"]
                sensitivity = max(result["window_tee_crossings_m"]) - min(result["window_tee_crossings_m"])
                content = (
                    f'<div class="images"><img loading="lazy" src="{root}/comparison.svg" '
                    'alt="Pink house ray and green hog ray on the same broom image">'
                    f'<img loading="lazy" src="{root}/hog.svg" '
                    'alt="Original hog crossing frame with tracked handle points"></div>'
                    f"<p>{result['fit_point_count']} fitted observations · "
                    f"crossing at {result['crossing_seconds']:.3f}s. "
                    f"Tee crossing: house {old:+.2f} m → hog {new:+.2f} m. "
                    "Positive is the thrower’s right.</p>"
                    f"<p>Changing the fitting window moves the tee crossing by {sensitivity:.2f} m.</p>"
                    f"<p>Observed support: {abs(result['observed_span_m'][0]):.2f} m before to "
                    f"{result['observed_span_m'][1]:.2f} m after the hog.</p>"
                )
            shots.append(
                f'<article id="{cid}"><h3>Throw {index + 1} · end {shot.get("end") or "?"}</h3>{content}'
                f'<button data-id="{cid}">{"Recheck" if ready else "Measure hog ray"}</button>'
                '<span class="status"></span></article>'
            )
        sections.append(f"<section><h2>{escape(player['name'])}</h2>{''.join(shots)}</section>")
    return (
        '<!doctype html><html><meta charset="utf-8"><title>Hog-line ray comparison</title>'
        "<style>body{font:16px system-ui;max-width:1100px;margin:auto;padding:24px;"
        "background:#edf3f3;color:#15323d}article{padding:18px;margin:16px 0;"
        "background:white;border-radius:12px}.images{display:flex;gap:16px;align-items:start}"
        ".images img{width:48%;max-height:430px;object-fit:contain}button{padding:9px;cursor:pointer}"
        ".status{margin-left:12px}p{line-height:1.5}@media(max-width:650px){"
        ".images{display:block}.images img{width:100%}}</style>"
        f'<body data-api="/api/games/{game}/hog-rays/">'
        f'<a href="/api/games/{game}/skill-estimates/report">Player report</a>'
        "<h1>Direction at the delivery hog line</h1>"
        '<p><b style="color:#009b56">Green dashed: hog-line tangent.</b> '
        '<b style="color:#d0009a">Pink: existing house ray.</b> '
        "Both extend straight to the far tee and share the original broom image. "
        "The second panel shows the hog crossing and measured handle positions.</p>"
        "<p>The direction is the tangent of a local quadratic fit to observations within one metre "
        "before and after the delivery hog. These experimental estimates reuse the reconstruction "
        "camera mapping. Handle height is approximated using nearby granite edges. "
        "Missing or ambiguous crossings stay unavailable. Broom distance has not been scored.</p>"
        '<button id="all">Measure remaining throws</button>'
        + "".join(sections)
        + '<script src="/static/hog-rays.js"></script></body></html>'
    )
