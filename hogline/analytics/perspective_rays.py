"""Source-pixel perspective overlays; inferred rays are always dotted."""

import base64
from html import escape

from hogline.db import Evidence
from hogline.media.frames import read_image_bytes
from hogline.perception.perspective_rays import HOG_FROM_TEE_M, project, ray_segments


def _image(db, frame_id, camera):
    with db.snapshot_session() as session:
        frame = session.get(Evidence, frame_id)
        if frame is None:
            raise ValueError("Perspective source frame is missing")
        data = read_image_bytes(db.settings.data_dir, frame.document["crops"][camera])
    return base64.b64encode(data).decode()


def _line(points, color, width=1.5, extra=""):
    coords = " ".join(f"{x:.2f},{y:.2f}" for x, y in points)
    return f'<polyline points="{coords}" fill="none" stroke="{color}" stroke-width="{width}" {extra}/>'


def perspective_svg(db, ray, view):
    if not ray.get("perspectives"):
        raise ValueError("This delivery has no perspective calibration")
    if view == "distance":
        return distance_svg(db, ray)
    panel = ray["perspectives"][view]
    camera = panel["camera"]
    width, height = camera["image_size"]
    background = _image(db, panel["frame_id"], view)
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height + 60}" role="img">',
        f"<title>{escape(view.replace('_', ' '))}: straight delivery ray on approximate ice plane</title>",
        '<defs><clipPath id="ice">'
        f'<rect y="{camera["tee_px"][1] - 40:.2f}" width="{width}" '
        f'height="{camera["ice_bottom_px"] - camera["tee_px"][1] + 40:.2f}"/>'
        "</clipPath></defs>",
        f'<image width="{width}" height="{height}" href="data:image/png;base64,{background}"/>',
        '<g clip-path="url(#ice)">',
    ]
    for segment in ray_segments(ray, camera):
        parts.append(_line(segment, "white", 4, 'opacity=".75" stroke-dasharray="5 5"'))
        parts.append(_line(segment, "#e0009a", 2, 'stroke-dasharray="5 5"'))
    for y, color in ((0, "#00cde5"), (HOG_FROM_TEE_M, "#ffb400")):
        parts.append(
            _line(
                project([[-2.375, y], [2.375, y]], camera["local_to_pixel"]),
                color,
                1,
                'stroke-dasharray="4 4"',
            )
        )
    # Tee crossing in this view: use the near intercept or the far extrapolation.
    own = camera["house_view"] == ray["origin_view"]
    target = ray["near_tee_x_m"] if own else ray["target_tee_x_m"]
    dot = project([[camera["transverse_sign"] * target, 0]], camera["local_to_pixel"])[0]
    if abs(target) <= 2.375 and 0 < dot[0] < width:
        parts.append(
            f'<circle cx="{dot[0]:.2f}" cy="{dot[1]:.2f}" r="3" '
            'fill="#e0009a" stroke="white" stroke-width="1.3"/>'
        )
    split = ray["house_split"]
    timing = "Partial delivery-house timing; full split unavailable"
    if split.get("elapsed_seconds"):
        timing = (
            f"Weight proxy: {split['elapsed_seconds']:.3f} s / 12 ft"
            f" · {split['nominal_mean_speed_mps']:.2f} m/s"
        )
    parts.extend(
        [
            "</g>",
            f'<rect y="{height}" width="{width}" height="60" fill="#15323d"/>',
            f'<text x="8" y="{height + 18}" fill="white" font-family="system-ui,sans-serif" '
            f'font-size="12">{escape(timing)}</text>',
            f'<text x="8" y="{height + 36}" fill="#c4d8df" font-family="system-ui,sans-serif" '
            'font-size="11">Dotted pink: straight ray · cyan: tee · gold: hog line</text>',
            f'<text x="8" y="{height + 51}" fill="#c4d8df" font-family="system-ui,sans-serif" '
            'font-size="10">Approximate ice map; ray beyond local landmarks is extrapolated.</text></svg>',
        ]
    )
    return "".join(parts)


def distance_svg(db, ray):
    cal = ray["perspective_calibration"]
    pair = cal["pairs"][min(2, len(cal["pairs"]) - 1)]
    views = ("perspective_left", "perspective_right")
    widths = [cal["cameras"][v]["image_size"][0] for v in views]
    height = max(cal["cameras"][v]["image_size"][1] for v in views)
    width = sum(widths)
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height + 44}" role="img">',
        "<title>Same delivered stone in both perspective views at one broadcast PTS</title>",
    ]
    offset = 0
    for v, w in zip(views, widths, strict=True):
        image = _image(db, pair["frame_id"], v)
        x, y = pair[v]
        parts.extend(
            [
                f'<g transform="translate({offset} 0)">',
                f'<image width="{w}" height="{height}" href="data:image/png;base64,{image}"/>',
                f'<circle cx="{x}" cy="{y}" r="15" fill="none" stroke="white" stroke-width="4"/>',
                f'<circle cx="{x}" cy="{y}" r="15" fill="none" stroke="#e0009a" stroke-width="2"/>',
                f'<path d="M {x - 8} {y} h 16" stroke="#e0009a" stroke-width="2"/>',
                "</g>",
            ]
        )
        offset += w
    parts.extend(
        [
            f'<rect y="{height}" width="{width}" height="44" fill="#15323d"/>',
            f'<text x="10" y="{height + 19}" fill="white" font-family="system-ui,sans-serif" '
            f'font-size="13">Same yellow stone · {pair["source_seconds"]:.3f} s broadcast time · '
            "marked bottom granite edge</text>",
            f'<text x="10" y="{height + 35}" fill="#c4d8df" font-family="system-ui,sans-serif" '
            'font-size="11">Camera exposure synchronisation is unverified. '
            "Original pixels retained.</text></svg>",
        ]
    )
    return "".join(parts)
