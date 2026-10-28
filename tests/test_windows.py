import subprocess

import pytest

from hogline.adapters.sources import import_local
from hogline.media.frames import source_seconds
from hogline.media.windows import decode_window, pop_png


def test_window_keeps_original_pts_from_single_seek(video):
    frames = list(decode_window(str(video), 0.4, 2.5, interval=0.5, max_frames=5))
    seconds = [t["pts"] * t["time_base_num"] / t["time_base_den"] for _, t in frames]
    assert seconds == pytest.approx([0.5, 1.0, 1.5, 2.0])
    assert all(image.size == (160, 90) for image, _ in frames)


def test_window_nonzero_media_start_preserves_source_mapping(db, video, tmp_path):
    shifted = tmp_path / "window-shifted.mp4"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(video), "-c", "copy", "-output_ts_offset", "7", str(shifted)],
        check=True,
    )
    source = import_local(db, str(shifted), 5)
    frames = list(decode_window(str(shifted), 0.4, 2.5, interval=0.5, max_frames=5, source_time_origin=7))
    assert [source_seconds(t, source) for _, t in frames] == pytest.approx([0.5, 1.0, 1.5, 2.0])
    assert frames[0][1]["pts"] * frames[0][1]["time_base_num"] / frames[0][1]["time_base_den"] == 7.5


def test_window_cancellation_stops_decoder(video):
    def cancel():
        raise ValueError("Operator cancelled")

    with pytest.raises(ValueError, match="Operator cancelled"):
        list(decode_window(str(video), 0, 2, tick=cancel))


def test_window_bounds_and_missing_media_do_not_invent_frames(tmp_path):
    with pytest.raises(ValueError, match="Invalid bounded"):
        list(decode_window("unused", 0, 61))
    with pytest.raises(ValueError, match="Invalid bounded"):
        list(decode_window("unused", 0, 60, interval=0.25, max_frames=5))
    with pytest.raises(ValueError, match="incomplete"):
        list(decode_window(str(tmp_path / "missing.mp4"), 0, 1))
    with pytest.raises(ValueError, match="invalid image framing"):
        pop_png(bytearray(b"corrupt framing"))


def test_variable_frame_spacing_is_preserved(video, tmp_path):
    variable = tmp_path / "variable.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(video),
            "-vf",
            "select='eq(n,0)+eq(n,4)+eq(n,11)+eq(n,20)'",
            "-fps_mode",
            "vfr",
            "-c:v",
            "libx264",
            str(variable),
        ],
        check=True,
    )
    frames = list(decode_window(str(variable), 0, 2.5, interval=0.5))
    assert [t["pts"] * t["time_base_num"] / t["time_base_den"] for _, t in frames] == pytest.approx(
        [0, 1.1, 2.0]
    )


def test_closing_window_generator_reaps_decoder(video, monkeypatch):
    from hogline.media import windows

    real_popen, processes = windows.subprocess.Popen, []

    def tracked(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(windows.subprocess, "Popen", tracked)
    stream = decode_window(str(video), 0, 3, interval=0.25)
    next(stream)
    stream.close()
    assert len(processes) == 1
    assert processes[0].poll() is not None
    assert processes[0].stdout.closed and processes[0].stderr.closed


def test_nested_sample_cadences_share_original_frames_across_offset_windows(video, tmp_path):
    import hashlib
    from fractions import Fraction

    thirty = tmp_path / "thirty-fps.mp4"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(video), "-vf", "fps=30", "-c:v", "libx264", str(thirty)],
        check=True,
    )
    dense = list(decode_window(str(thirty), 0, 3, interval=0.25, max_frames=12))
    sparse = list(decode_window(str(thirty), 0.4, 2.9, interval=0.5, max_frames=5))

    def keyed(frames):
        return {
            Fraction(t["pts"] * t["time_base_num"], t["time_base_den"]): hashlib.sha256(
                image.tobytes()
            ).hexdigest()
            for image, t in frames
        }

    a, b = keyed(dense), keyed(sparse)
    assert len(a) == 12  # No accumulating eight-frames-per-quarter-second drift.
    assert list(b) == [Fraction(n, 2) for n in range(1, 6)]
    assert all(a[pts] == sha for pts, sha in b.items())
    seconds = [float(t) for t in a]
    assert seconds[:5] == pytest.approx([0, 8 / 30, 0.5, 23 / 30, 1])
    assert max(abs(actual - index * 0.25) for index, actual in enumerate(seconds)) < 1 / 30


def test_source_grid_rejects_window_without_any_target_and_nonfinite_origin(video):
    with pytest.raises(ValueError, match="source-aligned sample"):
        list(decode_window(str(video), 0.1, 0.2, interval=0.5))
    with pytest.raises(ValueError, match="Invalid bounded"):
        list(decode_window(str(video), 0, 1, source_time_origin=float("nan")))
