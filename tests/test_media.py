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
    assert "landscape" in shorts_problem(MediaInfo(1920, 1080, 30), True, 180)
    assert shorts_problem(MediaInfo(1080, 1080, 30), True, 180) is None      # square counts as a Short


def test_drive_metadata_fallback():
    assert from_drive(None, 10, 10) is None
    info = from_drive(1080, 1920, 45000)
    assert info.vertical and info.duration_s == 45 and info.source == "drive"


@needs_ffmpeg
def test_av1_opus_low_res_is_transcoded_and_upscaled(tmp_path):
    from autopost.media import EncodeSettings, prepare_for_upload

    src = tmp_path / "in.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=240x426:rate=25",
                    "-f", "lavfi", "-i", "sine=frequency=440", "-t", "2", "-c:v", "libsvtav1", "-c:a", "libopus",
                    str(src)], check=True, capture_output=True)
    out, before, after = prepare_for_upload(src, tmp_path / "out", EncodeSettings())
    assert (before.vcodec, before.acodec) == ("av1", "opus")
    assert (after.vcodec, after.acodec, after.pix_fmt) == ("h264", "aac", "yuv420p")
    assert (after.width, after.height) == (720, 1280)
    # an already compatible, large enough file is used as-is
    again, _, _ = prepare_for_upload(out, tmp_path / "out2", EncodeSettings())
    assert again == out


@needs_ffmpeg
def test_landscape_upscale_box_and_no_audio(tmp_path):
    from autopost.media import EncodeSettings, prepare_for_upload

    src = make_video(tmp_path / "wide.mp4", "854x480")
    out, before, after = prepare_for_upload(src, tmp_path / "o", EncodeSettings())
    assert before.acodec is None and after.acodec is None
    assert (after.width, after.height) == (1280, 720)


def test_transcode_reasons():
    from autopost.media import EncodeSettings, transcode_reasons

    ok = MediaInfo(1080, 1920, 20, container="mov,mp4,m4a", vcodec="h264", pix_fmt="yuv420p", acodec="aac")
    assert transcode_reasons(ok, EncodeSettings()) == []
    bad = MediaInfo(240, 426, 20, container="matroska,webm", vcodec="av1", pix_fmt="yuv420p", acodec="opus")
    assert len(transcode_reasons(bad, EncodeSettings())) == 4


class FakeRun:
    def __init__(self, results, installs_on=None):
        self.calls = []
        self.results = list(results)
        self.installs_on = installs_on

    def __call__(self, args, **kw):
        self.calls.append(args)
        rc = self.results.pop(0) if self.results else 0
        return subprocess.CompletedProcess(args, rc, stdout="", stderr="boom" if rc else "")


def _patch(monkeypatch, which, run, clock):
    import autopost.media as m

    monkeypatch.setattr(m.shutil, "which", which)
    monkeypatch.setattr(m.subprocess, "run", run)
    monkeypatch.setattr(m.time, "monotonic", clock)


def test_ensure_ffmpeg_already_present(monkeypatch):
    from autopost.media import ensure_ffmpeg

    run = FakeRun([])
    _patch(monkeypatch, lambda name: f"/usr/bin/{name}", run, lambda: 0)
    assert ensure_ffmpeg() == "present" and run.calls == []


def test_ensure_ffmpeg_from_cached_debs(monkeypatch, tmp_path):
    from autopost.media import ensure_ffmpeg

    (tmp_path / "ffmpeg_6.1.deb").write_bytes(b"x")
    state = {"installed": False}
    run = FakeRun([0])

    def which(name):
        if name in ("ffmpeg", "ffprobe"):
            return "/usr/bin/ffmpeg" if state["installed"] else None
        return f"/usr/bin/{name}"

    def run_and_install(args, **kw):
        state["installed"] = True
        return run(args, **kw)

    _patch(monkeypatch, which, run_and_install, iter([0, 5]).__next__)
    assert ensure_ffmpeg(tmp_path) == "cache"
    assert run.calls[0][:3] == ["sudo", "-n", "dpkg"]


def test_ensure_ffmpeg_falls_back_to_apt_and_alerts_when_slow(monkeypatch, tmp_path):
    from autopost.media import ensure_ffmpeg

    state = {"installed": False}
    run = FakeRun([0, 0, 0])
    alerts = []

    def which(name):
        if name in ("ffmpeg", "ffprobe"):
            return "/usr/bin/ffmpeg" if state["installed"] else None
        return f"/usr/bin/{name}"

    def fake_run(args, **kw):
        if "install" in args:
            state["installed"] = True
        return run(args, **kw)

    _patch(monkeypatch, which, fake_run, iter([0, 200]).__next__)
    assert ensure_ffmpeg(tmp_path / "cache", alert=alerts.append) == "apt"
    install = next(c for c in run.calls if "install" in c)
    assert f"Dir::Cache::archives={tmp_path / 'cache'}" in install
    assert alerts and "3.3 min" in alerts[0]


@needs_ffmpeg
def test_codec_only_keeps_size_and_quality_but_moves_moov_to_front(tmp_path):
    from autopost.media import EncodeSettings, prepare_for_upload

    src = tmp_path / "reel.mp4"          # ffmpeg writes the moov atom at the end by default
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=360x640:rate=30",
                    "-f", "lavfi", "-i", "sine=sample_rate=44100", "-t", "2", "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(src)], check=True)
    out, before, after = prepare_for_upload(src, tmp_path / "out", EncodeSettings(transcode="codec_only", upscale=False))
    assert before.faststart is False and after.faststart is True
    assert (after.width, after.height, after.vcodec, after.sample_rate) == (360, 640, "h264", 44100)
    assert abs(out.stat().st_size - src.stat().st_size) < 4096         # remux only, no re-encode
