"""Branded frame around a reel: logo bar on top, source credit at the bottom, the video in between.

The static layer (cream background, logo, credit line, a gold rounded border and a transparent
hole exactly the size of the scaled video) is drawn once per video with Pillow, then ffmpeg scales
the video into the hole and overlays the layer in a single re-encode pass. The output already
matches the upload spec (H.264 + AAC, yuv420p, constant fps, +faststart).
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .edits import EditPlan, ProgressStyle
from . import progress as prog
from .media import MediaInfo, ProbeError, TranscodeError, probe

log = logging.getLogger(__name__)

SUPERSAMPLE = 4
# grain and sparks are expensive to encode, so a reel with a decorative overlay gets a bitrate ceiling
OVERLAY_MAXRATE = "14M"


@dataclass(frozen=True)
class FrameStyle:
    width: int = 1080
    height: int = 1920
    top_bar: int = 270               # room for the logo
    bottom_bar: int = 130            # room for the credit line
    side_margin: int = 30            # minimum cream margin left/right of the video
    radius: int = 40                 # rounded corners of the video window
    border: int = 5                  # gold line around the window
    bg: str = "#fbf6ed"              # same cream as the logo's own background
    navy: str = "#092e48"
    gold: str = "#a47a3c"
    logo: Path | None = None
    credit: str | None = None
    font: Path | None = None
    fps: int = 30
    crf: int = 18
    preset: str = "veryfast"
    audio_bitrate: str = "160k"


def _even(n: float) -> int:
    return max(2, int(n) // 2 * 2)


def video_rect(style: FrameStyle, vw: int, vh: int) -> tuple[int, int, int, int]:
    """(x, y, w, h) of the video window: the source fitted into the area between the bars."""
    area_w = style.width - 2 * style.side_margin
    area_h = style.height - style.top_bar - style.bottom_bar
    scale = min(area_w / vw, area_h / vh)
    w, h = _even(vw * scale), _even(vh * scale)
    x = (style.width - w) // 2 // 2 * 2
    y = (style.top_bar + (area_h - h) // 2) // 2 * 2
    return x, y, w, h


def _draw_arabic(draw, xy, text: str, font, fill, anchor_width: int | None = None) -> tuple[int, int]:
    """Draw right-to-left text. Pillow with libraqm shapes and orders Arabic itself; without it the
    letters are joined with arabic_reshaper and reordered with python-bidi first. Returns (w, h)."""
    from PIL import features

    if features.check("raqm"):
        kw = {"direction": "rtl", "language": "ar"}
    else:
        import arabic_reshaper
        from bidi.algorithm import get_display
        from PIL import ImageFont

        # visual-order text drawn left to right: the basic layout engine must not reorder it a second time
        font = ImageFont.truetype(font.path, font.size, layout_engine=ImageFont.Layout.BASIC)
        text, kw = get_display(arabic_reshaper.reshape(text)), {}
    box = draw.textbbox((0, 0), text, font=font, **kw)
    w, h = box[2] - box[0], box[3] - box[1]
    if xy is not None:
        x, y = xy
        draw.text((x - box[0] + ((anchor_width - w) // 2 if anchor_width else 0), y - box[1]), text,
                  font=font, fill=fill, **kw)
    return w, h


def _prepare_logo(logo, bg: str):
    """Logo as RGBA on a transparent background: its own cream turns transparent (so no lighter box
    shows on the frame), and the empty margin around it is cropped away."""
    from PIL import Image, ImageChops

    rgb = logo.convert("RGB")
    corner = Image.new("RGB", rgb.size, rgb.getpixel((2, 2)))
    diff = ImageChops.difference(rgb, corner).convert("L")
    box = diff.point(lambda v: 255 if v > 24 else 0).getbbox()
    alpha = diff.point(lambda v: min(255, v * 6))          # partial alpha keeps anti-aliased edges soft
    out = rgb.convert("RGBA")
    out.putalpha(alpha)
    return out.crop(box) if box else out


def logo_box(style: FrameStyle) -> tuple[int, int, int, int] | None:
    """(x0, y0, x1, y1) of the logo as render_layer places it, or None without a logo."""
    from PIL import Image

    if not (style.logo and Path(style.logo).exists()):
        return None
    logo = _prepare_logo(Image.open(style.logo), style.bg)
    W = style.width
    scale = min((style.top_bar - 50) / logo.height, (W - 2 * style.side_margin) / logo.width)
    lw, lh = max(1, round(logo.width * scale)), max(1, round(logo.height * scale))
    x0, y0 = (W - lw) // 2, (style.top_bar - lh) // 2
    return x0, y0, x0 + lw, y0 + lh


def render_pattern_mask(style: FrameStyle, rect: tuple[int, int, int, int]):
    """Grey image that lets the geometric pattern show only on the plain cream: never on the video window
    and its gold border, never behind the logo, never behind the credit line. Soft edges, so the pattern
    fades out instead of being cut."""
    from PIL import Image, ImageDraw, ImageFilter

    W, H = style.width, style.height
    x, y, w, h = rect
    m = Image.new("L", (W, H), 255)
    d = ImageDraw.Draw(m)
    gap = style.border + 14
    d.rounded_rectangle([x - gap, y - gap, x + w + gap, y + h + gap], radius=style.radius + gap, fill=0)
    box = logo_box(style)
    if box:
        x0, y0, x1, y1 = box
        d.rounded_rectangle([x0 - 40, y0 - 24, x1 + 40, y1 + 24], radius=40, fill=0)
    if style.credit:
        d.rectangle([W // 2 - 270, H - style.bottom_bar, W // 2 + 270, H], fill=0)
    return m.filter(ImageFilter.GaussianBlur(10))


def render_layer(style: FrameStyle, rect: tuple[int, int, int, int]):
    """RGBA image: opaque everywhere except the (rounded) video window, which is transparent."""
    from PIL import Image, ImageDraw, ImageFont

    W, H = style.width, style.height
    x, y, w, h = rect
    layer = Image.new("RGBA", (W, H), style.bg)
    draw = ImageDraw.Draw(layer)

    # logo, centred in the top bar
    if style.logo and Path(style.logo).exists():
        logo = _prepare_logo(Image.open(style.logo), style.bg)
        target_h = style.top_bar - 50
        scale = min(target_h / logo.height, (W - 2 * style.side_margin) / logo.width)
        logo = logo.resize((max(1, round(logo.width * scale)), max(1, round(logo.height * scale))), Image.LANCZOS)
        layer.alpha_composite(logo, ((W - logo.width) // 2, (style.top_bar - logo.height) // 2))

    # credit line, centred in the bottom bar
    if style.credit:
        font_path = style.font or "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        font = ImageFont.truetype(str(font_path), 40)
        # thin gold rule with a small diamond between the window and the credit
        ry = H - style.bottom_bar + 26
        draw.line([(W // 2 - 220, ry), (W // 2 - 18, ry)], fill=style.gold, width=3)
        draw.line([(W // 2 + 18, ry), (W // 2 + 220, ry)], fill=style.gold, width=3)
        draw.polygon([(W // 2, ry - 9), (W // 2 + 9, ry), (W // 2, ry + 9), (W // 2 - 9, ry)], fill=style.gold)
        _, th = _draw_arabic(draw, None, style.credit, font, style.navy)
        _draw_arabic(draw, (0, ry + (style.bottom_bar - 26 - th) // 2 - 4), style.credit, font, style.navy,
                     anchor_width=W)

    # gold ring around the window, then punch the transparent hole (both anti-aliased via supersampling)
    s = SUPERSAMPLE
    ring = Image.new("L", (W * s, H * s), 0)
    ImageDraw.Draw(ring).rounded_rectangle(
        [(x - style.border) * s, (y - style.border) * s, (x + w + style.border) * s - 1, (y + h + style.border) * s - 1],
        radius=(style.radius + style.border) * s, fill=255)
    hole = Image.new("L", (W * s, H * s), 0)
    ImageDraw.Draw(hole).rounded_rectangle([x * s, y * s, (x + w) * s - 1, (y + h) * s - 1],
                                           radius=style.radius * s, fill=255)
    ring = ring.resize((W, H), Image.LANCZOS)
    hole = hole.resize((W, H), Image.LANCZOS)
    gold = Image.new("RGBA", (W, H), style.gold)
    layer = Image.composite(gold, layer, ring)
    alpha = hole.point(lambda v: 255 - v)
    layer.putalpha(alpha)
    return layer


def input_order(plan: EditPlan) -> dict[str, int]:
    """Which ffmpeg input index each piece gets. The order here is the order `render_video` passes them in."""
    order = ["src"] + (["hook"] if plan.hook else [])
    order += ["overlay", "mask"] if plan.overlay else []
    order += (["outro"] if plan.outro else []) + (["pattern", "pmask"] if plan.pattern else [])
    order += (["ring"] if plan.progress else []) + ["layer"]
    return {name: i for i, name in enumerate(order)}


def ring_geometry(style: FrameStyle, rect: tuple[int, int, int, int], ps: ProgressStyle):
    """(offset, size, inset, radius) of the progress ring: the gold border's own path, with room for the glow."""
    x, y, w, h = rect
    pad = max(0, min(ps.glow * 2 + ps.width, x - style.border, y - style.border))
    return ((x - style.border - pad, y - style.border - pad),
            (w + 2 * (style.border + pad), h + 2 * (style.border + pad)),
            pad + style.border / 2, style.radius + style.border // 2)


def render_protect_mask(size: tuple[int, int], clear: float, power: float = 1.7):
    """Grey image that multiplies a decorative overlay: black over the middle `clear` of the picture, where
    the speaker is, ramping to white at the edges. Keeps any clip - ours or one the user dropped in - off
    the face and off any text burned into the reel."""
    import numpy as np
    from PIL import Image

    w, h = size
    if clear <= 0:
        return Image.new("RGB", (w, h), (255, 255, 255))      # no protection: the effect covers everything
    ys = np.linspace(-1.0, 1.0, h, dtype=np.float32)[:, None]
    xs = np.linspace(-1.0, 1.0, w, dtype=np.float32)[None, :]
    r = np.sqrt(xs ** 2 + ys ** 2) / np.sqrt(2.0)
    t = np.clip((r - clear) / max(1e-3, 1.0 - clear), 0.0, 1.0) ** power
    return Image.fromarray((t * 255).astype(np.uint8), mode="L").convert("RGB")


def frame_filter(style: FrameStyle, rect: tuple[int, int, int, int], plan: EditPlan | None = None,
                 audio: bool = True, ring: "prog.ProgressSpec | None" = None,
                 outro_audio: bool = False) -> str:
    """The filter graph. Outputs [out], plus [aout] when the audio had to be cut together."""
    x, y, w, h = rect
    plan = plan or EditPlan()
    idx = input_order(plan)
    base = f"scale={w}:{h}:flags=lanczos,setsar=1,fps={style.fps},format=yuv420p"
    zw, zh = _even(w * plan.zoom_factor), _even(h * plan.zoom_factor)
    punch = f"scale={zw}:{zh}:flags=lanczos,setsar=1,fps={style.fps},format=yuv420p,crop={w}:{h}"
    afmt = "aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo"
    cut_audio = audio and (plan.hook is not None or plan.outro is not None)
    parts = []

    if plan.zoom:
        # the punched-in copy lies over the normal one only during the zoom spans (hard cuts, like an edit)
        on = "+".join(f"between(t,{a:.2f},{b:.2f})" for a, b in plan.zoom)
        parts.append(f"[0:v]{base},split[n0][n1];[n1]scale={zw}:{zh}:flags=lanczos,crop={w}:{h}[z];"
                     f"[n0][z]overlay=0:0:enable=\'{on}\'[main]")
    else:
        parts.append(f"[0:v]{base}[main]")

    if plan.hook is not None:
        # the opening sentence is shown punched in (when zoom is on), so the reel's own start reads as a cut
        parts.append(f"[{idx['hook']}:v]{punch if plan.zoom else base},setpts=PTS-STARTPTS[hook]")
        if audio:
            parts.append(f"[{idx['hook']}:a:0]{afmt},asetpts=PTS-STARTPTS,afade=t=in:d=0.06,"
                         f"afade=t=out:st={max(0.0, plan.hook_seconds - 0.1):.2f}:d=0.1[ha];[0:a:0]{afmt}[ma];"
                         f"[hook][ha][main][ma]concat=n=2:v=1:a=1[cut][ba]")
        else:
            parts.append("[hook][main]concat=n=2:v=1:a=0[cut]")
    else:
        parts.append("[main]null[cut]")
        if cut_audio:
            parts.append(f"[0:a:0]{afmt}[ba]")

    if plan.overlay is not None:
        # both blends run on planar RGB: `screen` on YUV would lift the colour difference planes too and
        # tint the whole picture. The mask keeps the effect away from the middle.
        o = plan.overlay
        parts.append(f"[{idx['overlay']}:v]scale={w}:{h}:flags=lanczos,setsar=1,fps={style.fps},format=gbrp[ov];"
                     f"[{idx['mask']}:v]scale={w}:{h}:flags=lanczos,setsar=1,fps={style.fps},format=gbrp[mk];"
                     f"[ov][mk]blend=all_mode=multiply:shortest=1[ovm];[cut]format=gbrp[cutr];"
                     f"[cutr][ovm]blend=all_mode={o.blend}:all_opacity={o.opacity:.3f}:shortest=1,"
                     f"format=yuv420p[body]")
    else:
        parts.append("[cut]null[body]")

    if plan.outro is not None:
        speed = plan.outro.speed
        pts = f",setpts=PTS/{speed:.4f}" if abs(speed - 1.0) > 1e-3 else ""
        parts.append(f"[{idx['outro']}:v]scale={w}:{h}:flags=lanczos:force_original_aspect_ratio=decrease,"
                     f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color={style.bg.replace('#', '0x')},setsar=1,"
                     f"fps={style.fps},format=yuv420p{pts},setpts=PTS-STARTPTS[otv]")
        if audio:
            if outro_audio:
                tempo = f",atempo={speed:.4f}" if abs(speed - 1.0) > 1e-3 else ""
                parts.append(f"[{idx['outro']}:a:0]{afmt}{tempo},asetpts=PTS-STARTPTS[oa]")
            else:
                parts.append(f"anullsrc=channel_layout=stereo:sample_rate=48000:"
                             f"d={plan.outro.seconds:.3f}[oa]")
            parts.append("[body][ba][otv][oa]concat=n=2:v=1:a=1[vid][aout]")
        else:
            parts.append("[body][otv]concat=n=2:v=1:a=0[vid]")
    else:
        parts.append("[body]null[vid]")
        if cut_audio:
            parts.append("[ba]anull[aout]")

    parts.append(f"color=c={style.bg.replace('#', '0x')}:s={style.width}x{style.height}:r={style.fps},"
                 f"format=yuv420p[bg];[bg][vid]overlay={x}:{y}:shortest=1[comp];"
                 f"[comp][{idx['layer']}:v]overlay=0:0:format=auto:shortest=1[framed0]")

    if plan.pattern is not None:
        # white-on-black clip -> gold lines on the cream only (the mask keeps it off the window, logo, credit)
        pt = plan.pattern
        parts.append(f"[{idx['pattern']}:v]scale={style.width}:{style.height},fps={style.fps},format=gray[pg];"
                     f"[{idx['pmask']}:v]scale={style.width}:{style.height},fps={style.fps},format=gray[pk];"
                     f"[pg][pk]blend=all_mode=multiply:shortest=1,lut=c0=val*{pt.opacity:.3f}[pa];"
                     f"color=c={pt.color.replace('#', '0x')}:s={style.width}x{style.height}:r={style.fps},"
                     f"format=rgba[pc];[pc][pa]alphamerge[pat];"
                     f"[framed0][pat]overlay=0:0:shortest=1[framed]")
    else:
        parts.append("[framed0]null[framed]")

    if ring is not None:
        rx, ry = ring.offset
        rw, rh = ring.size
        parts.append(f"color=c={ring.color.replace('#', '0x')}:s={rw}x{rh}:r={style.fps},format=rgba[rc];"
                     f"[{idx['ring']}:v]format=gray,fps={style.fps},tpad=stop=-1:stop_mode=clone[rm];"
                     f"[rc][rm]alphamerge[ring];"
                     f"[framed][ring]overlay={rx}:{ry}:shortest=1,format=yuv420p[out]")
    else:
        parts.append("[framed]format=yuv420p[out]")
    return ";".join(parts)


def render_video(src: Path, out: Path, style: FrameStyle, info: MediaInfo, work_dir: Path,
                 timeout_seconds: int = 3600, plan: EditPlan | None = None) -> Path:
    """Re-encode `src` inside the frame into `out` (one ffmpeg pass). `plan` adds the cold open, the zoom
    cuts, the decorative overlay clip, the progress ring and the closing logo clip."""
    rect = video_rect(style, info.width, info.height)
    plan = plan or EditPlan()
    layer_path = work_dir / f"{src.stem}.frame.png"
    render_layer(style, rect).save(layer_path)
    mask_path = work_dir / f"{src.stem}.mask.png"
    ring_dir = work_dir / f"{src.stem}.ring"
    ring = None
    content_seconds = info.duration_s + plan.hook_seconds      # the ring closes with the reel, not the logo
    if plan.progress is not None:
        offset, size, inset, radius = ring_geometry(style, rect, plan.progress)
        ring = prog.render_sequence(ring_dir, size, inset, radius, plan.progress.width, plan.progress.glow,
                                    prog.step_count(content_seconds), content_seconds, plan.progress.color,
                                    offset)

    inputs = ["-i", str(src)]
    if plan.hook is not None:
        inputs += ["-ss", f"{plan.hook[0]:.3f}", "-t", f"{plan.hook_seconds:.3f}", "-i", str(src)]
    if plan.overlay is not None:
        render_protect_mask((rect[2], rect[3]), plan.overlay.protect_center).save(mask_path)
        inputs += ["-stream_loop", "-1", "-i", str(plan.overlay.path),
                   "-framerate", str(style.fps), "-loop", "1", "-i", str(mask_path)]
    outro_audio = False
    if plan.outro is not None:
        try:
            outro_audio = probe(plan.outro.path).acodec is not None
        except ProbeError:
            outro_audio = False
        inputs += ["-i", str(plan.outro.path)]
    pmask_path = work_dir / f"{src.stem}.pmask.png"
    if plan.pattern is not None:
        render_pattern_mask(style, rect).save(pmask_path)
        inputs += ["-stream_loop", "-1", "-i", str(plan.pattern.path),
                   "-framerate", str(style.fps), "-loop", "1", "-i", str(pmask_path)]
    if ring is not None:
        inputs += ["-framerate", str(ring.fps), "-i", str(ring.pattern)]
    inputs += ["-framerate", str(style.fps), "-loop", "1", "-i", str(layer_path)]

    has_audio = info.acodec is not None
    cut_audio = has_audio and (plan.hook is not None or plan.outro is not None)
    audio_map = ["-map", "[aout]"] if cut_audio else ["-map", "0:a:0?"]
    gop = style.fps * 2
    cmd = ["ffmpeg", "-v", "error", "-y", *inputs,
           "-filter_complex", frame_filter(style, rect, plan, has_audio, ring, outro_audio),
           "-map", "[out]", *audio_map,
           "-c:v", "libx264", "-preset", style.preset, "-crf", str(style.crf), "-profile:v", "high",
           "-level:v", "4.1", "-pix_fmt", "yuv420p", "-g", str(gop), "-keyint_min", str(gop), "-sc_threshold", "0",
           "-c:a", "aac", "-b:a", style.audio_bitrate, "-ar", "48000", "-ac", "2",
           "-movflags", "+faststart", "-shortest", str(out)]
    if plan.overlay is not None:
        cmd[-1:-1] = ["-maxrate", OVERLAY_MAXRATE, "-bufsize", "28M"]
    notes = (f"; cold open {plan.hook[0]:.1f}-{plan.hook[1]:.1f}s" if plan.hook else "") + (
        f"; {len(plan.zoom)} zoom cut(s) x{plan.zoom_factor}" if plan.zoom else "") + (
        f"; overlay {plan.overlay.path.name} at {plan.overlay.opacity:.0%}" if plan.overlay else "") + (
        "; progress ring" if ring else "") + (
        f"; outro {plan.outro.path.name} x{plan.outro.speed:.2f} ({plan.outro.seconds:.1f}s)"
        if plan.outro else "") + ("; moving pattern" if plan.pattern else "")
    log.info("framing %s: window %dx%d at (%d,%d) in %dx%d%s", src.name, rect[2], rect[3], rect[0], rect[1],
             style.width, style.height, notes)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        out.unlink(missing_ok=True)
        raise TranscodeError(f"ffmpeg (frame) timed out after {timeout_seconds}s") from exc
    finally:
        layer_path.unlink(missing_ok=True)
        mask_path.unlink(missing_ok=True)
        pmask_path.unlink(missing_ok=True)
        shutil.rmtree(ring_dir, ignore_errors=True)
    if result.returncode != 0:
        out.unlink(missing_ok=True)
        raise TranscodeError(f"ffmpeg (frame) failed: {result.stderr.strip()[-400:]}")
    return out
