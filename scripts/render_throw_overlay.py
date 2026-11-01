"""Render cached, aligned team throw paths for a standalone visual review/video."""

import argparse
import json
import subprocess
from pathlib import Path

import numpy as np
from PIL import Image, ImageColor, ImageDraw, ImageFont

from hogline.config import Settings
from hogline.db import Database
from hogline.orchestration.throw_reconstructions import catalog

COLORS = ["#3bdec0", "#ff9b83", "#83baff", "#d9a1ff"]


def render(game_id, output):
    data = catalog(Database(Settings.load()), game_id)
    rows = [r for r in data["throws"] if r["state"] == "ready"]
    if len(rows) != len(data["throws"]):
        raise ValueError("Finish generating all selected throws before exporting the full team replay")
    output.mkdir(parents=True, exist_ok=True)
    (output / "catalog.json").write_text(json.dumps(data))
    distance = 34.7472
    width, height, fps = 1280, 1080, 24
    scale = (width - 64) / (distance + 8)
    players = data["players"]
    font_path = Path("/System/Library/Fonts/Supplemental/Arial.ttf")

    def font(n):
        return ImageFont.truetype(str(font_path), n) if font_path.exists() else ImageFont.load_default(size=n)

    fonts = {n: font(n) for n in (11, 15, 18, 22, 30)}

    def xy(x, s):
        return 32 + (s + 4) * scale, 104 + x * scale

    def sheet(label):
        im = Image.new("RGBA", (width, 184), (20, 45, 58, 255))
        d = ImageDraw.Draw(im)
        d.text((20, 7), label, font=fonts[18], fill="#edf5f7")
        cy = 104
        d.rectangle((20, cy - 2.375 * scale, width - 20, cy + 2.375 * scale), fill="#edf3f3")
        for s in (0, distance):
            cx, _ = xy(0, s)
            for radius, color in (
                (1.8288, "#67a9c9"),
                (1.2192, "#edf3f3"),
                (0.6096, "#abc888"),
                (0.1524, "#edf3f3"),
            ):
                rr = radius * scale
                d.ellipse((cx - rr, cy - rr, cx + rr, cy + rr), fill=color)
        d.line((20, cy, width - 20, cy), fill="#bdccd0")
        for s in (6.4008, distance - 6.4008):
            x, _ = xy(0, s)
            d.line((x, cy - 2.375 * scale, x, cy + 2.375 * scale), fill="#cc6874", width=2)
        return im

    panels = [sheet(f"Whole team · {len(rows)} throws")]
    panels += [
        sheet(f"{p['name']} · {sum(r['person_id'] == p['person_id'] for r in rows)} throws") for p in players
    ]
    trails = [Image.new("RGBA", im.size) for im in panels]
    row_info = []
    for r in rows:
        index = next(i for i, p in enumerate(players) if p["person_id"] == r["person_id"])
        color = ImageColor.getrgb(COLORS[index % len(COLORS)])
        row_info.append(
            {
                "row": r,
                "panel": index + 1,
                "color": color,
                "grid": np.array(r["trajectory"]["grid"]),
                "cursor": 1,
                "support": np.array(r["trajectory"]["observation_times"]),
            }
        )
    duration = max(r["trajectory"]["duration_seconds"] for r in rows) + 2
    encoder = subprocess.Popen(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-s",
            f"{width}x{height}",
            "-r",
            str(fps),
            "-i",
            "pipe:0",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(output / "team-replay.mp4"),
        ],
        stdin=subprocess.PIPE,
    )
    try:
        for frame_number in range(int(np.ceil(duration * fps))):
            t = frame_number / fps
            for info in row_info:
                grid = info["grid"]
                while info["cursor"] < len(grid) and grid[info["cursor"], 0] <= t:
                    i = info["cursor"]
                    a, b = grid[i - 1], grid[i]
                    supported = np.min(abs(info["support"] - (a[0] + b[0]) / 2)) < 0.35
                    if supported or i % 2 == 0:
                        for panel in (0, info["panel"]):
                            ImageDraw.Draw(trails[panel]).line(
                                [xy(*a[1:]), xy(*b[1:])], fill=(*info["color"], 85), width=2
                            )
                    info["cursor"] += 1
            composed = [Image.alpha_composite(a, b) for a, b in zip(panels, trails, strict=True)]
            for info in row_info:
                tr = info["row"]["trajectory"]
                grid = info["grid"]
                alpha = (
                    255
                    if t <= grid[-1, 0] or tr["status"] in ("observed_rest", "apparent_rest")
                    else int(255 * max(0, 1 - (t - grid[-1, 0]) / 2))
                )
                if not alpha:
                    continue
                p = [float(np.interp(t, grid[:, 0], grid[:, k])) for k in (1, 2)]
                x, y = xy(*p)
                for panel in (0, info["panel"]):
                    layer = Image.new("RGBA", panels[panel].size)
                    d = ImageDraw.Draw(layer)
                    fill = (
                        info["color"]
                        if t <= grid[-1, 0] or tr["status"] in ("observed_rest", "apparent_rest")
                        else (237, 243, 243)
                    )
                    d.ellipse(
                        (x - 7, y - 7, x + 7, y + 7),
                        fill=(*fill, alpha),
                        outline=(15, 35, 44, alpha),
                        width=1,
                    )
                    d.text(
                        (x, y),
                        str(info["row"]["shot_number"]),
                        font=fonts[11],
                        anchor="mm",
                        fill=(15, 35, 44, alpha),
                    )
                    composed[panel] = Image.alpha_composite(composed[panel], layer)
            frame = Image.new("RGB", (width, height), "#0d202b")
            d = ImageDraw.Draw(frame)
            d.text((20, 17), "ALL THROWS, AT THE SAME TIME", font=fonts[30], fill="#edf5f7")
            d.text(
                (20, 59),
                f"{t:05.2f} s · aligned at the delivery tee · real elapsed time · both directions normalized",
                font=fonts[18],
                fill="#b5cad3",
            )
            for i, panel in enumerate(composed):
                frame.paste(panel.convert("RGB"), (0, 100 + i * 187))
            d = ImageDraw.Draw(frame)
            d.text(
                (20, 1051),
                "Dashed: inferred gaps · partial/contact tracks fade · approximate camera reconstructions",
                font=fonts[15],
                fill="#b5cad3",
            )
            encoder.stdin.write(frame.tobytes())
            if frame_number == fps * 12:
                frame.save(output / "team-replay.png")
            if frame_number % (fps * 10) == 0:
                print(f"Rendered {t:g} / {duration:.1f} s", flush=True)
    finally:
        encoder.stdin.close()
        encoder.wait()
    if encoder.returncode:
        raise ValueError("Team replay encoder failed")
    print(output / "team-replay.mp4", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("game_id")
    parser.add_argument("--output", type=Path, default=Path("data/reconstructions/team-review"))
    args = parser.parse_args()
    render(args.game_id, args.output)
