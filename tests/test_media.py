import subprocess

import pytest

from autopost.media import MediaInfo, ffprobe_available, from_drive, probe, shorts_problem

needs_ffmpeg = pytest.mark.skipif(not ffprobe_available(), reason="ffmpeg not installed")


def make_video(path, size, seconds=2, rotation=None):
    src = path.with_suffix(".src.mp4")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=size={size}:rate=10",
                    "-t", str(seconds), "-c:v", "libx264", "-pix_fmt", "yuv420p", str(src)], check=True)
    if rotation is None:
        src.rename(path)
    else:
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-display_rotation", str(rotation), "-i", str(src),
                        "-c", "copy", str(path)], check=True)
    return path


@needs_ffmpeg
def test_probe_vertical_and_landscape(tmp_path):
    v = probe(make_video(tmp_path / "v.mp4", "360x640"))
    assert (v.width, v.height, v.vertical) == (360, 640, True)
    assert abs(v.duration_s - 2) < 0.2
    h = probe(make_video(tmp_path / "h.mp4", "640x360"))
    assert not h.vertical


@needs_ffmpeg
def test_probe_applies_rotation(tmp_path):
    r = probe(make_video(tmp_path / "r.mp4", "640x360", rotation=90))
    assert r.vertical and r.rotation in (90, -90)


def test_shorts_rules():
    assert shorts_problem(MediaInfo(1080, 1920, 59), True, 180) is None
    assert shorts_problem(MediaInfo(1080, 1920, 180), True, 180) is None
    assert "longer than 180s" in shorts_problem(MediaInfo(1080, 1920, 181), True, 180)
    assert "not vertical" in shorts_problem(MediaInfo(1920, 1080, 30), True, 180)
    assert "not vertical" in shorts_problem(MediaInfo(1080, 1080, 30), True, 180)


def test_drive_metadata_fallback():
    assert from_drive(None, 10, 10) is None
    info = from_drive(1080, 1920, 45000)
    assert info.vertical and info.duration_s == 45 and info.source == "drive"
