import json
import random
import shutil
import subprocess
from pathlib import Path

import pytest

from autopost.cover import pick_cover
from autopost.frame import FrameStyle, render_layer, render_video, video_rect
from autopost.media import MediaInfo, probe

ROOT = Path(__file__).resolve().parents[1]
needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg is not installed")


def style(**kw):
    return FrameStyle(logo=ROOT / "assets/kalima_sawaa/logo.png", credit="المصدر: معاذ عليان",
                      font=ROOT / "assets/fonts/DejaVuSans-Bold.ttf", **kw)


def test_window_fits_a_vertical_video_between_the_bars():
    x, y, w, h = video_rect(style(), 720, 1280)
    assert w / h == pytest.approx(720 / 1280, rel=0.01)
    assert x % 2 == 0 and y % 2 == 0 and w % 2 == 0 and h % 2 == 0
    assert y >= style().top_bar and y + h <= 1920 - style().bottom_bar and x >= style().side_margin


def test_window_for_landscape_and_square_sources():
    s = style()
    for vw, vh in ((1920, 1080), (1080, 1080), (1080, 1920)):
        x, y, w, h = video_rect(s, vw, vh)
        assert x >= s.side_margin and x + w <= s.width - s.side_margin
        assert y >= s.top_bar and y + h <= s.height - s.bottom_bar
        assert w / h == pytest.approx(vw / vh, rel=0.02)


def test_layer_is_transparent_only_inside_the_window():
    s = style()
    rect = video_rect(s, 720, 1280)
    layer = render_layer(s, rect)
    x, y, w, h = rect
    assert layer.size == (1080, 1920)
    assert layer.getpixel((x + w // 2, y + h // 2))[3] == 0          # video shows through
    assert layer.getpixel((5, 5))[3] == 255                          # cream margin is opaque
    assert layer.getpixel((540, 1920 - 40))[3] == 255                # credit bar is opaque


def make_video(path: Path, seconds=4, size="720x1280", audio=True):
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=duration={seconds}:size={size}:rate=25"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p"] + (["-c:a", "aac", "-shortest"] if audio else []) + [str(path)]
    subprocess.run(cmd, check=True)
    return path


@needs_ffmpeg
def test_render_video_outputs_a_1080x1920_upload_ready_file(tmp_path):
    src = make_video(tmp_path / "in.mp4")
    out = render_video(src, tmp_path / "out.mp4", style(preset="ultrafast"), probe(src), tmp_path)
    info = probe(out)
    assert (info.width, info.height) == (1080, 1920)
    assert (info.vcodec, info.acodec, info.pix_fmt, info.faststart) == ("h264", "aac", "yuv420p", True)
    assert info.duration_s == pytest.approx(4, abs=0.4) and info.fps == pytest.approx(30, abs=0.5)
    assert not list(tmp_path.glob("*.frame.png"))                       # the temporary layer is removed


@needs_ffmpeg
def test_render_video_without_audio_and_with_a_landscape_source(tmp_path):
    src = make_video(tmp_path / "land.mp4", size="1280x720", audio=False)
    out = render_video(src, tmp_path / "out.mp4", style(preset="ultrafast"), probe(src), tmp_path)
    info = probe(out)
    assert (info.width, info.height, info.acodec) == (1080, 1920, None)


@needs_ffmpeg
def test_cover_is_a_usable_frame_inside_the_video(tmp_path):
    src = make_video(tmp_path / "in.mp4", seconds=6)
    cover = pick_cover(src, 6.0, tmp_path, rng=random.Random(1))
    assert cover is not None and cover.image_path.exists() and cover.image_path.suffix == ".jpg"
    assert 1000 <= cover.offset_ms <= 4800                              # 20-80% of 6 s, 1 s away from the ends


@needs_ffmpeg
def test_cover_skips_black_frames(tmp_path):
    src = tmp_path / "black-then-picture.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=black:s=360x640:d=5:r=25",
                    "-f", "lavfi", "-i", "testsrc2=duration=5:size=360x640:rate=25",
                    "-filter_complex", "[0][1]concat=n=2:v=1:a=0", "-pix_fmt", "yuv420p", str(src)], check=True)
    for seed in range(5):
        cover = pick_cover(src, 10.0, tmp_path, rng=random.Random(seed))
        assert cover.offset_ms > 5000                                    # never the black first half


def test_arabic_credit_renders_the_same_with_and_without_raqm(monkeypatch):
    """Without libraqm (e.g. fribidi missing on a runner) the fallback must give the same picture."""
    from PIL import ImageChops, ImageStat, features

    s = style()
    rect = video_rect(s, 720, 1280)
    with_raqm = render_layer(s, rect).crop((0, 1790, 1080, 1920)).convert("L")
    real_check = features.check
    monkeypatch.setattr(features, "check", lambda name, *a, **k: False if name == "raqm" else real_check(name))
    without = render_layer(s, rect).crop((0, 1790, 1080, 1920)).convert("L")
    diff = ImageChops.difference(with_raqm, without)
    assert ImageStat.Stat(diff).mean[0] < 3                # same text in the same place (tiny anti-aliasing noise)


@needs_ffmpeg
def test_drive_content_frames_the_video_and_picks_a_cover(app, tmp_path):
    from test_drive_source import make

    content, _, client = make(app, tmp_path)
    src = make_video(tmp_path / "source.mp4", seconds=5)
    client.download = lambda fid, dest, size: (dest.parent.mkdir(parents=True, exist_ok=True),
                                               shutil.copy(src, dest), dest)[2]
    app.brands["kalima_sawaa"].streams["short"].frame.crf = 30             # faster test encode
    content.encode = content.encode.__class__(**{**content.encode.__dict__, "preset": "ultrafast"})
    from autopost.content.base import VideoItem

    video = VideoItem(id="r1", name="عنوان.mp4", created=None)
    content._stream_of["r1"] = ("kalima_sawaa", "short")
    path = content.fetch(video)
    video.local_path = path
    assert (content.probe(video).width, content.probe(video).height) == (1080, 1920)
    cover = content.cover(video)
    assert cover is not None and cover.image_path.exists() and 1000 <= cover.offset_ms <= 4000
    content.release(video)
    assert content.cover(video) is None and not any(p.suffix in (".mp4", ".jpg") for p in (tmp_path / "media").iterdir())


# ------------------------------------------------------------------ edits: cold open + zoom cuts

from autopost.edits import (EditPlan, HookRules, Segment, hook_candidates, hook_window, segments_from_json,  # noqa: E402
                            segments_to_json, zoom_windows)
from autopost.frame import frame_filter  # noqa: E402

SEGS = [Segment(0.0, 3.0, "مقدمة قصيرة عن الموضوع هنا"), Segment(3.2, 7.5, "كلام تمهيدي قبل السؤال الأساسي"),
        Segment(9.0, 12.0, "هل النص ده معناه اللي بيتقال"), Segment(12.4, 13.0, "طيب"),
        Segment(14.0, 24.0, "شرح طويل " * 8), Segment(25.0, 28.0, "وده الرد على السؤال اللي فات")]


def test_hook_candidates_skip_the_start_short_long_and_tiny_segments():
    picked = hook_candidates(SEGS, HookRules())
    assert [s.start for s in picked] == [9.0, 25.0]
    assert hook_window(picked[0], HookRules(), 30.0) == pytest.approx((8.92, 12.15))
    assert hook_window(Segment(29.0, 29.95, "x"), HookRules(), 30.0)[1] == 30.0     # never past the end


def test_zoom_windows_alternate_on_sentence_starts_and_respect_the_minimum_shot():
    windows = zoom_windows(SEGS, 30.0, min_shot=4.0)
    assert windows == [(9.0, 14.0), (25.0, 28.0)]            # cuts at 9, 14, 25: zoomed, normal, zoomed
    assert zoom_windows([], 30.0) == [] and zoom_windows(SEGS[:1], 30.0) == []
    for a, b in windows:
        assert 0 < a < b <= 30.0


def test_segments_round_trip_through_state_json():
    assert segments_from_json(segments_to_json(SEGS)) == SEGS


def test_filter_without_a_plan_has_no_extra_inputs():
    rect = video_rect(style(), 720, 1280)
    graph = frame_filter(style(), rect)
    assert "[1:v]overlay" in graph and "concat" not in graph and "enable" not in graph
    graph = frame_filter(style(), rect, EditPlan(hook=(9.0, 12.0), zoom=[(9.0, 14.0)]))
    assert "[2:v]overlay" in graph and "concat=n=2:v=1:a=1" in graph and "between(t,9.00,14.00)" in graph


@needs_ffmpeg
def test_render_with_cold_open_and_zoom_is_longer_by_the_hook(tmp_path):
    src = make_video(tmp_path / "in.mp4", seconds=8)
    plan = EditPlan(hook=(5.0, 7.0), zoom=[(2.0, 4.0)], zoom_factor=1.1)
    out = render_video(src, tmp_path / "out.mp4", style(preset="ultrafast"), probe(src), tmp_path, plan=plan)
    info = probe(out)
    assert (info.width, info.height, info.vcodec, info.acodec) == (1080, 1920, "h264", "aac")
    assert info.duration_s == pytest.approx(10, abs=0.4)


@needs_ffmpeg
def test_render_with_cold_open_on_a_video_without_sound(tmp_path):
    src = make_video(tmp_path / "in.mp4", seconds=6, audio=False)
    out = render_video(src, tmp_path / "out.mp4", style(preset="ultrafast"), probe(src), tmp_path,
                       plan=EditPlan(hook=(3.0, 5.0)))
    info = probe(out)
    assert info.acodec is None and info.duration_s == pytest.approx(8, abs=0.4)
