"""Actual FFmpeg → OpenCV → Parquet → tracklets, with known synthetic truth."""

import shutil
import subprocess

import cv2
import numpy as np
import pytest

from hogline.adapters.sources import import_local
from hogline.analytics.exports import snapshot
from hogline.analytics.moments import MomentQuery, search_moments
from hogline.analytics.tracks import derive_window_tracks, load_track_input
from hogline.domain.catalog import catalog
from hogline.orchestration import windows, worker


def write_motion_video(path, width):
    height, crop = width * 9 // 16, width // 4
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "-s",
        f"{width}x{height}",
        "-r",
        "20",
        "-i",
        "pipe:0",
        "-an",
        "-c:v",
        "ffv1",
        str(path),
    ]
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        for i in range(140):
            rgb = np.full((height, width, 3), 245, dtype=np.uint8)
            left = width * 3 // 8
            for top in (0, height - crop):
                center = (left + crop // 2, top + crop // 2)
                cv2.circle(rgb, center, round(crop * 0.42), (30, 60, 200), -1)
                cv2.circle(rgb, center, round(crop * 0.30), (245, 245, 245), -1)
                # Two handles inside the house: red moves; yellow remains stationary.
                for x, y, color in (
                    (0.30 + i / 139 * 0.35, 0.45, (230, 20, 20)),
                    (0.38, 0.63, (240, 210, 20)),
                ):
                    p = (left + round(x * crop), top + round(y * crop))
                    cv2.circle(rgb, p, round(crop * 0.023), (130, 130, 130), -1)
                    cv2.circle(rgb, p, round(crop * 0.016), color, -1)
            # A foreground obstruction interrupts only the top view from 3–4 seconds.
            if 60 <= i < 80:
                cv2.rectangle(
                    rgb,
                    (left + round(crop * 0.35), round(crop * 0.2)),
                    (left + round(crop * 0.65), round(crop * 0.75)),
                    (35, 35, 35),
                    -1,
                )
            process.stdin.write(rgb.tobytes())
        process.stdin.close()
        error = process.stderr.read().decode()
        assert process.wait(timeout=30) == 0, error
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


@pytest.mark.parametrize("width", [1280, 1920])
def test_real_decoder_detector_and_tracks_preserve_motion_and_occlusion(db, tmp_path, width):
    if not shutil.which("ffmpeg"):
        pytest.skip("FFmpeg is required")
    path = tmp_path / "synthetic-motion.mkv"
    write_motion_video(path, width)
    source = import_local(db, str(path), sheet=3)
    windows.enqueue_window(
        db,
        windows.WindowRequest(
            source_id=source["id"],
            start_seconds=0,
            end_seconds=7,
            interval_seconds=0.25,
            max_frames=28,
            max_evidence_mb=16,
        ),
    )
    job = worker.run_one(db)
    assert job["state"] == "succeeded", job
    data = catalog(db)
    assert not data["games"]
    window = data["analysis_windows"][0]
    rows = load_track_input(db.settings.data_dir, window)
    assert window["sample_count"] == 28
    assert len(rows) == 56
    result = derive_window_tracks(db, window["id"])
    red = [t for t in result["tracks"] if t["color"] == "red"]
    assert len(red) == 3
    top = [t for t in red if t["view"] == "house_top"]
    assert len(top) == 2
    assert all(t["end_seconds"] < 3 or t["start_seconds"] >= 4 for t in top)
    bottom = next(t for t in red if t["view"] == "house_bottom")
    assert len(bottom["points"]) == 28
    assert all(t["motion_class"] == "position_change_observed" for t in red)
    assert all(
        t["motion_class"] == "small_net_displacement" for t in result["tracks"] if t["color"] == "yellow"
    )
    for track in result["tracks"]:
        for point in track["points"]:
            assert point["source_seconds"] == pytest.approx(
                point["pts"] * point["time_base_num"] / point["time_base_den"]
            )
            assert point["youtube_url"] is None  # Local video must never invent a YouTube identity.
            if track["color"] == "red":
                expected_x = width / 4 * (0.30 + point["source_seconds"] * 20 / 139 * 0.35)
                assert point["center_px"][0] == pytest.approx(expected_x, abs=1)
    release = snapshot(db, "exploratory")
    assert release["window_tracks"][0]["sha256"] == result["sha256"]
    found = search_moments(db, MomentQuery(kinds=["handle_motion"]))
    assert found["total"] == 3
    assert all(m["start_youtube_url"] is None for m in found["moments"])
