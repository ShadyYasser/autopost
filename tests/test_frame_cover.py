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


# ------------------------------------------------------------------ overlay clip + progress ring

from autopost.edits import OutroSpec, OverlaySpec, ProgressStyle  # noqa: E402
from autopost.frame import input_order, ring_geometry  # noqa: E402
from autopost.progress import path_points, render_sequence, step_count, sweep_angles  # noqa: E402

OVERLAY = ROOT / "assets/kalima_sawaa/overlays/embers.mp4"
OUTRO = ROOT / "assets/kalima_sawaa/outro.mp4"


def test_input_order_matches_what_render_video_passes():
    assert input_order(EditPlan()) == {"src": 0, "layer": 1}
    full = EditPlan(hook=(1, 2), overlay=OverlaySpec(OVERLAY), progress=ProgressStyle(),
                    outro=OutroSpec(OUTRO, 10.0))
    assert input_order(full) == {"src": 0, "hook": 1, "overlay": 2, "mask": 3, "outro": 4, "ring": 5,
                                 "layer": 6}
    assert input_order(EditPlan(progress=ProgressStyle())) == {"src": 0, "ring": 1, "layer": 2}
    assert input_order(EditPlan(overlay=OverlaySpec(OVERLAY))) == {"src": 0, "overlay": 1, "mask": 2, "layer": 3}


def test_filter_blends_the_overlay_and_draws_the_ring():
    s, rect = style(), video_rect(style(), 720, 1280)
    graph = frame_filter(s, rect, EditPlan(overlay=OverlaySpec(OVERLAY, opacity=0.12, blend="screen")))
    assert "blend=all_mode=screen:all_opacity=0.120" in graph and "blend=all_mode=multiply" in graph
    assert "format=gbrp" in graph and "[1:v]scale=" in graph and "[2:v]scale=" in graph
    assert "alphamerge" not in frame_filter(s, rect, EditPlan(progress=ProgressStyle()), ring=None)


def test_ring_sits_on_the_border_and_stays_inside_the_frame():
    s = style()
    for vw, vh in ((720, 1280), (1920, 1080), (1080, 1080)):
        rect = video_rect(s, vw, vh)
        (ox, oy), (rw, rh), inset, radius = ring_geometry(s, rect, ProgressStyle())
        assert ox >= 0 and oy >= 0 and ox + rw <= s.width and oy + rh <= s.height
        assert inset > 0 and radius >= s.radius
        assert (ox + rw // 2, oy + rh // 2) == (rect[0] + rect[2] // 2, rect[1] + rect[3] // 2)   # centred


def test_the_sweep_moves_at_a_steady_speed_along_the_path():
    size, inset, radius = (900, 1500), 24.0, 42
    angles = sweep_angles(size, inset, radius, 200)
    assert angles[0] > -90 and angles[-1] == 270 and angles == sorted(angles)
    # quarter of the path length -> the middle of the long right side, which is due east of the centre
    assert angles[49] == pytest.approx(0, abs=4) and angles[99] == pytest.approx(90, abs=4)
    assert angles[149] == pytest.approx(180, abs=4)
    pts = path_points(size, inset, radius)
    assert pts[0] == pts[-1] == (size[0] / 2, inset)                      # a closed lap from the top middle


def test_ring_frames_only_ever_light_up_more_of_the_same_ring(tmp_path):
    import numpy as np
    from PIL import Image

    spec = render_sequence(tmp_path / "ring", (300, 500), 12, 20, 5, 3, 12, 6.0, "#ffd98a")
    assert spec.steps == 12 and spec.fps == 2 and spec.pattern.parent.is_dir()
    frames = [np.asarray(Image.open(spec.pattern.parent / ("ring%05d.png" % i)), dtype=np.int16)
              for i in range(1, 13)]
    for a, b in zip(frames, frames[1:]):
        assert (b >= a).all()                                              # never un-lights a pixel
    assert frames[0].max() > 0 and frames[0].sum() < frames[-1].sum() / 4


def test_step_count_is_bounded():
    assert step_count(1) == 90 and step_count(40) == 400 and step_count(5000) == 600


@needs_ffmpeg
def test_render_with_overlay_and_ring_keeps_the_upload_spec(tmp_path):
    src = make_video(tmp_path / "in.mp4", seconds=6)
    plan = EditPlan(overlay=OverlaySpec(OVERLAY, opacity=0.1), progress=ProgressStyle())
    out = render_video(src, tmp_path / "out.mp4", style(preset="ultrafast"), probe(src), tmp_path, plan=plan)
    info = probe(out)
    assert (info.width, info.height, info.vcodec, info.acodec) == (1080, 1920, "h264", "aac")
    assert info.duration_s == pytest.approx(6, abs=0.4)
    assert not list(tmp_path.glob("*.ring"))                               # the frame sequence is cleaned up


@needs_ffmpeg
def test_the_ring_is_brighter_at_the_end_than_at_the_start(tmp_path):
    import numpy as np
    from PIL import Image

    src = make_video(tmp_path / "in.mp4", seconds=8, audio=False)
    out = render_video(src, tmp_path / "out.mp4", style(preset="ultrafast"), probe(src), tmp_path,
                       plan=EditPlan(progress=ProgressStyle()))
    rect = video_rect(style(), 720, 1280)
    (ox, oy), (rw, rh), _, _ = ring_geometry(style(), rect, ProgressStyle())
    lit = []
    for at in (0.2, 7.6):
        shot = tmp_path / f"{at}.png"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(at), "-i", str(out), "-frames:v", "1",
                        "-vf", f"crop={rw}:30:{ox}:{oy}", str(shot)], check=True)   # the top edge of the ring
        lit.append(np.asarray(Image.open(shot).convert("L"), dtype=np.float32).mean())
    assert lit[1] > lit[0] + 2                                             # the lap closed over the top edge


# ------------------------------------------------------------------ protect mask + closing logo clip

from autopost.frame import render_protect_mask  # noqa: E402


def test_the_protect_mask_blacks_out_the_middle_and_opens_up_at_the_edges():
    import numpy as np

    m = np.asarray(render_protect_mask((400, 700), 0.45).convert("L"), dtype=np.float32)
    assert m[350, 200] == 0                                            # dead centre: no effect at all
    assert m[0, 0] > 230 and m[-1, -1] > 230                           # corners: full effect
    rows = [m[350, 200 + d] for d in range(0, 200, 20)]
    assert rows == sorted(rows)                                        # never gets darker on the way out
    wide = np.asarray(render_protect_mask((400, 700), 0.1).convert("L"), dtype=np.float32)
    assert wide[350, 380] > m[350, 380]                                # a smaller guard opens up sooner
    off = np.asarray(render_protect_mask((40, 40), 0.0).convert("L"), dtype=np.float32)
    assert (off == 255).all()                                          # 0 = no protection anywhere


def test_the_outro_is_concatenated_and_gets_silence_when_it_has_none():
    s, rect = style(), video_rect(style(), 720, 1280)
    plan = EditPlan(outro=OutroSpec(OUTRO, 10.0, 2.0))
    graph = frame_filter(s, rect, plan, audio=True, outro_audio=False)
    assert "setpts=PTS/2.0000" in graph and "anullsrc" in graph and "d=5.000" in graph
    assert "concat=n=2:v=1:a=1[vid][aout]" in graph
    heard = frame_filter(s, rect, plan, audio=True, outro_audio=True)
    assert "anullsrc" not in heard and "atempo=2.0000" in heard
    silent = frame_filter(s, rect, plan, audio=False)
    assert "concat=n=2:v=1:a=0[vid]" in silent and "aout" not in silent


def test_an_outro_at_normal_speed_is_not_resampled():
    graph = frame_filter(style(), video_rect(style(), 720, 1280), EditPlan(outro=OutroSpec(OUTRO, 4.0, 1.0)))
    assert "setpts=PTS/" not in graph and "atempo" not in graph


@needs_ffmpeg
def test_render_appends_the_sped_up_outro(tmp_path):
    src = make_video(tmp_path / "in.mp4", seconds=5)
    out = render_video(src, tmp_path / "out.mp4", style(preset="ultrafast"), probe(src), tmp_path,
                       plan=EditPlan(outro=OutroSpec(OUTRO, probe(OUTRO).duration_s, 2.0)))
    info = probe(out)
    assert info.duration_s == pytest.approx(5 + probe(OUTRO).duration_s / 2, abs=0.4)
    assert (info.width, info.height, info.acodec) == (1080, 1920, "aac")


@needs_ffmpeg
def test_the_overlay_never_touches_the_middle_and_adds_no_colour_cast(tmp_path):
    """A pure black overlay clip must change nothing: `screen` run on YUV would shift the colour planes
    even where the clip is black, tinting the whole reel."""
    import numpy as np
    from PIL import Image

    src, black = tmp_path / "flat.mp4", tmp_path / "black.mp4"
    for path, color in ((src, "0x808080"), (black, "black")):
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c={color}:s=720x1280:d=4",
                        "-c:v", "libx264", "-crf", "10", "-pix_fmt", "yuv420p", str(path)], check=True)
    rect = video_rect(style(), 720, 1280)

    def render(name, plan):
        return render_video(src, tmp_path / f"{name}.mp4", style(preset="ultrafast", crf=12), probe(src),
                            tmp_path, plan=plan)

    def area(video, x, y):
        shot = tmp_path / f"{video.stem}-{x}.png"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", "2", "-i", str(video), "-frames:v", "1",
                        "-vf", f"crop=140:140:{x}:{y}", str(shot)], check=True)
        return np.asarray(Image.open(shot).convert("RGB"), dtype=np.float32).reshape(-1, 3).mean(axis=0)

    plain = render("plain", None)
    guarded = render("guarded", EditPlan(overlay=OverlaySpec(OVERLAY, opacity=0.1, protect_center=0.45)))
    cx, cy = rect[0] + rect[2] // 2 - 70, rect[1] + rect[3] // 2 - 70
    assert np.abs(area(guarded, cx, cy) - area(plain, cx, cy)).max() < 1.5      # the speaker's area is safe

    dark = render("dark", EditPlan(overlay=OverlaySpec(black, opacity=0.3, protect_center=0.0)))
    corner = (rect[0] + 20, rect[1] + 20)
    before, after = area(plain, *corner), area(dark, *corner)
    assert np.abs(after - before).max() < 2                                    # no lift and no colour cast


# ------------------------------------------------------------------ moving pattern in the cream margins

from autopost.edits import PatternSpec  # noqa: E402
from autopost.frame import logo_box, render_pattern_mask  # noqa: E402

PATTERN = ROOT / "assets/kalima_sawaa/pattern.mp4"


def test_the_pattern_mask_keeps_off_the_window_the_logo_and_the_credit():
    import numpy as np

    s = style()
    rect = video_rect(s, 1080, 1920)
    m = np.asarray(render_pattern_mask(s, rect), dtype=np.float32)
    x, y, w, h = rect
    assert m[y + h // 2, x + w // 2] == 0                              # the video window
    assert m[y - s.border - 2, x + w // 2] < 30                         # its gold border
    x0, y0, x1, y1 = logo_box(s)
    assert m[(y0 + y1) // 2, (x0 + x1) // 2] == 0                       # the logo
    assert m[1920 - 40, 540] == 0                                       # the credit line
    assert m[y + h // 2, x // 2] > 200                                  # the side margin shows it
    assert m[60, 40] > 200                                              # the top corner shows it


def test_the_pattern_comes_after_the_frame_and_before_the_ring():
    s, rect = style(), video_rect(style(), 720, 1280)
    plan = EditPlan(pattern=PatternSpec(PATTERN, opacity=0.3), progress=ProgressStyle())
    assert input_order(plan) == {"src": 0, "pattern": 1, "pmask": 2, "ring": 3, "layer": 4}
    graph = frame_filter(s, rect, plan)
    assert "lut=c0=val*0.300" in graph and "alphamerge[pat]" in graph
    assert graph.index("[framed0][pat]overlay") < graph.index("[framed]format=yuv420p")


@needs_ffmpeg
def test_render_with_the_pattern_leaves_the_video_untouched(tmp_path):
    import numpy as np
    from PIL import Image

    src = tmp_path / "flat.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=0x404850:s=720x1280:d=3",
                    "-c:v", "libx264", "-crf", "12", "-pix_fmt", "yuv420p", str(src)], check=True)
    rect = video_rect(style(), 720, 1280)
    shots = {}
    for name, plan in (("plain", None), ("pat", EditPlan(pattern=PatternSpec(PATTERN, opacity=0.6)))):
        out = render_video(src, tmp_path / f"{name}.mp4", style(preset="ultrafast", crf=12), probe(src),
                           tmp_path, plan=plan)
        shot = tmp_path / f"{name}.png"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", "1", "-i", str(out), "-frames:v", "1", str(shot)],
                       check=True)
        shots[name] = np.asarray(Image.open(shot).convert("L"), dtype=np.float32)
    x, y, w, h = rect
    inside = (slice(y + 20, y + h - 20), slice(x + 20, x + w - 20))
    assert np.abs(shots["pat"][inside] - shots["plain"][inside]).max() < 3      # the reel is not touched
    margin = (slice(y, y + h), slice(0, x - 20))
    assert np.abs(shots["pat"][margin] - shots["plain"][margin]).max() > 20     # the margin has the pattern
    assert not list(tmp_path.glob("*.pmask.png"))
