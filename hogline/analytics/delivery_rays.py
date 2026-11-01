"""Source-image overlays in original camera coordinates, using the fitted ice plane."""

import base64
import io
from html import escape

import numpy as np
from PIL import Image

from hogline.db import Evidence
from hogline.media.frames import read_image_bytes
from hogline.perception.delivery_ray import image_points


def ray_svg(db, ray, view):
    if view in ("perspective_left", "perspective_right", "distance"):
        from hogline.analytics.perspective_rays import perspective_svg

        return perspective_svg(db, ray, view)
    origin = view == "origin"
    frame_id = ray["origin_frame_id"] if origin else ray["receiving_frame_id"]
    camera = ray["origin_view"] if origin else ray["receiving_view"]
    geometry = ray["geometry"] if origin else ray["receiving_geometry"]
    with db.snapshot_session() as session:
        frame = session.get(Evidence, frame_id)
        data = read_image_bytes(db.settings.data_dir, frame.document["crops"][camera])
    with Image.open(io.BytesIO(data)) as im:
        width, height = im.size
    background = base64.b64encode(data).decode()
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height + 48}" role="img">',
        f"<title>{'Delivery-house native track' if origin else 'Receiving-house straight ray'}</title>",
        f'<defs><clipPath id="ice"><rect width="{width}" height="{height}"/></clipPath></defs>',
        f'<image width="{width}" height="{height}" href="data:image/png;base64,{background}"/>',
        '<g clip-path="url(#ice)">',
    ]

    def line(points, color, width=1.5, extra=""):
        coords = " ".join(f"{x:.2f},{y:.2f}" for x, y in points)
        return f'<polyline points="{coords}" fill="none" stroke="{color}" stroke-width="{width}" {extra}/>'

    if origin:
        pts = [p["pixel"] for p in ray["points"]]
        parts.append(line(pts, "#ff00b7", 1.8))
        for x, y in pts[::6]:
            parts.append(
                f'<circle cx="{x:.2f}" cy="{y:.2f}" r="1.7" fill="#fff" stroke="#9c005f" stroke-width=".7"/>'
            )
        for s in (-1.8288, 1.8288):
            parts.append(
                line(
                    image_points([[-2.3, ray["forward_sign"] * s], [2.3, ray["forward_sign"] * s]], geometry),
                    "#ffb400",
                    1,
                    'stroke-dasharray="4 3"',
                )
            )
        caption = f"{ray['point_count']} native observations · 60 Hz source"
    else:
        target = ray["target_tee_x_m"]
        slope = ray["slope_transverse_per_forward"]
        sg = ray["forward_sign"]
        ys = np.linspace(-2.2, 2.2, 40)
        # Same image X handedness at both ends; receiving y has the travel sign.
        points = image_points([[target + slope * sg * y, y] for y in ys], geometry)
        parts.append(line(points, "#ff00b7", 2.2))
        tee = image_points([[-3, 0], [3, 0]], geometry)
        parts.append(line(tee, "#00cde5", 1, 'stroke-dasharray="4 3"'))
        band = image_points([[v, 0] for v in ray["target_tee_sensitivity_m"]], geometry)
        parts.append(line(band, "#ff00b7", 10, 'opacity=".25"'))
        dot = image_points([[target, 0]], geometry)[0]
        x, y = dot
        if 5 <= x <= width - 5 and 5 <= y <= height - 5:
            parts.append(
                f'<circle cx="{x:.2f}" cy="{y:.2f}" r="4" fill="#ff00b7" stroke="white" stroke-width="1.5"/>'
            )
        else:
            edge = 8 if x < 0 else width - 8
            parts.append(
                f'<path d="M {edge} {y:.2f} l {8 if x < 0 else -8} -6 v 12 z" fill="#ff00b7" stroke="white"/>'
            )
        caption = f"Tee crossing {abs(target):.2f} m image-{'right' if target >= 0 else 'left'}"
        weight = (
            "Light of tee (observed)"
            if ray["observed_weight"]["status"] == "light_of_tee"
            else "Weight forecast unavailable"
        )
        parts.extend(
            [
                '<rect x="6" y="5" width="205" height="20" rx="3" fill="white" opacity=".9"/>',
                f'<text x="12" y="19" fill="#15323d" font-family="system-ui,sans-serif" '
                f'font-size="11">{weight}</text>',
            ]
        )
    legend = "Gold: house entry / exit planes" if origin else "Pink: ray / sensitivity · cyan: tee line"
    parts.extend(
        [
            "</g>",
            f'<rect y="{height}" width="{width}" height="48" fill="#15323d"/>',
            f'<text x="8" y="{height + 19}" fill="white" font-family="system-ui,sans-serif" '
            f'font-size="11">{escape(caption)}</text>',
            f'<text x="8" y="{height + 36}" fill="#c4d8df" font-family="system-ui,sans-serif" '
            f'font-size="10">{legend}</text></svg>',
        ]
    )
    return "".join(parts)
