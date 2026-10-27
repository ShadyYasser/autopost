"""Video inspection (ffprobe) and preparation for upload (ffmpeg).

Every platform gets the same prepared file: H.264 + AAC in MP4 (yuv420p, +faststart).
Files that already meet that and are big enough are used as-is; otherwise they are
transcoded once per post. Small videos (short side under `min_short_side`) are upscaled
with lanczos into a 720x1280 / 1280x720 / 720x720 box, keeping the aspect ratio and padding.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class MediaInfo:
    width: int              # as displayed (rotation applied)
    height: int
    duration_s: float
    rotation: int = 0
    source: str = "ffprobe"  # ffprobe | drive
    container: str | None = None
    vcodec: str | None = None
    pix_fmt: str | None = None
    acodec: str | None = None   # None when there is no audio stream
    size: int | None = None
    fps: float | None = None
    profile: str | None = None
    sar: str | None = None
    sample_rate: int | None = None
    channels: int | None = None
    faststart: bool | None = None   # moov atom before mdat

    @property
    def vertical(self) -> bool:
        """Portrait or square (YouTube treats square videos as Shorts too)."""
        return self.height >= self.width

    @property
    def short_side(self) -> int:
        return min(self.width, self.height)

    def describe(self) -> str:
        parts = [f"{self.width}x{self.height}", f"{self.duration_s:.1f}s"]
        if self.vcodec:
            parts.append(f"{self.vcodec}/{self.pix_fmt or '?'}")
        if self.source == "ffprobe":
            parts.append(f"audio {self.acodec or 'none'}")
        if self.container:
            parts.append("mp4" if "mp4" in self.container.split(",") else self.container.split(",")[0])
        if self.size:
            parts.append(f"{self.size / 1_048_576:.1f} MB")
        if self.fps:
            parts.append(f"{self.fps:g}fps")
        if self.profile:
            parts.append(self.profile)
        if self.sar and self.sar not in ("1:1", "0:1"):
            parts.append(f"SAR {self.sar}")
        if self.sample_rate:
            parts.append(f"{self.sample_rate}Hz/{self.channels}ch")
        if self.faststart is not None:
            parts.append("faststart" if self.faststart else "moov-at-end")
        if self.rotation:
            parts.append(f"rot {self.rotation}")
        return " ".join(parts)


@dataclass(frozen=True)
class EncodeSettings:
    min_short_side: int = 720
    crf: int = 20
    preset: str = "veryfast"
    audio_bitrate: str = "128k"
    timeout_seconds: int = 3600
    max_fps: int = 30


class ProbeError(Exception):
    pass


class TranscodeError(Exception):
    pass


def ffprobe_available() -> bool:
    return shutil.which("ffprobe") is not None


def probe(path: str | Path) -> MediaInfo:
    if not ffprobe_available():
        raise ProbeError("ffprobe is not installed")
    cmd = ["ffprobe", "-v", "error",
           "-show_entries",
           "stream=codec_type,codec_name,profile,pix_fmt,width,height,avg_frame_rate,r_frame_rate,"
           "sample_aspect_ratio,sample_rate,channels:stream_side_data=rotation:stream_tags=rotate"
           ":format=format_name,duration,size",
           "-of", "json", str(path)]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise ProbeError(f"ffprobe failed: {result.stderr.strip()[:300]}")
    data = json.loads(result.stdout or "{}")
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if video is None:
        raise ProbeError("no video stream")
    width, height = int(video.get("width") or 0), int(video.get("height") or 0)
    rotation = 0
    for side in video.get("side_data_list") or []:
        if "rotation" in side:
            rotation = int(float(side["rotation"]))
    if not rotation and (video.get("tags") or {}).get("rotate"):
        rotation = int(float(video["tags"]["rotate"]))
    if abs(rotation) % 180 == 90:
        width, height = height, width
    fmt = data.get("format") or {}
    try:
        duration = float(fmt.get("duration") or 0)
    except ValueError:
        duration = 0.0
    if not (width and height and duration):
        raise ProbeError(f"incomplete probe result: {width}x{height} {duration}s")
    return MediaInfo(width, height, duration, rotation, "ffprobe", fmt.get("format_name"),
                     video.get("codec_name"), video.get("pix_fmt"), audio.get("codec_name") if audio else None,
                     int(fmt["size"]) if fmt.get("size") else None,
                     fps=_rate(video.get("avg_frame_rate")) or _rate(video.get("r_frame_rate")),
                     profile=video.get("profile"), sar=video.get("sample_aspect_ratio"),
                     sample_rate=int(audio["sample_rate"]) if audio and audio.get("sample_rate") else None,
                     channels=audio.get("channels") if audio else None,
                     faststart=moov_before_mdat(path))


def _rate(value: str | None) -> float | None:
    try:
        num, den = (value or "0/0").split("/")
        return round(int(num) / int(den), 3) if int(den) else None
    except ValueError:
        return None


def moov_before_mdat(path: str | Path) -> bool | None:
    """True if the MP4 index (moov) comes before the media data (mdat), i.e. "+faststart"."""
    try:
        with open(path, "rb") as fh:
            offset = 0
            while True:
                fh.seek(offset)
                header = fh.read(16)
                if len(header) < 8:
                    return None
                size = int.from_bytes(header[:4], "big")
                kind = header[4:8]
                if kind == b"moov":
                    return True
                if kind == b"mdat":
                    return False
                if size == 1:
                    size = int.from_bytes(header[8:16], "big")
                if size < 8:
                    return None
                offset += size
    except OSError:
        return None


def from_drive(width: int | None, height: int | None, duration_ms: int | None) -> MediaInfo | None:
    """Drive's videoMediaMetadata (may ignore rotation; only a fallback/preview)."""
    if not (width and height and duration_ms):
        return None
    return MediaInfo(int(width), int(height), int(duration_ms) / 1000, source="drive")


def shorts_problem(info: MediaInfo, require_vertical: bool, max_seconds: int) -> str | None:
    """Why this video cannot go out as a YouTube Short, or None if it can (portrait or square)."""
    if require_vertical and not info.vertical:
        return f"landscape ({info.width}x{info.height})"
    if info.duration_s > max_seconds:
        return f"longer than {max_seconds}s ({info.duration_s:.0f}s)"
    return None


def transcode_reasons(info: MediaInfo, settings: EncodeSettings) -> list[str]:
    reasons = []
    if info.vcodec != "h264":
        reasons.append(f"video codec {info.vcodec}")
    if info.pix_fmt not in ("yuv420p", "yuvj420p"):
        reasons.append(f"pixel format {info.pix_fmt}")
    if info.acodec is not None and info.acodec != "aac":
        reasons.append(f"audio codec {info.acodec}")
    if "mp4" not in (info.container or "").split(","):
        reasons.append(f"container {info.container}")
    if info.short_side < settings.min_short_side:
        reasons.append(f"short side {info.short_side}px < {settings.min_short_side}px")
    if info.fps and info.fps > settings.max_fps + 0.5:
        reasons.append(f"{info.fps:g} fps > {settings.max_fps}")
    if info.sar not in (None, "1:1", "0:1", "N/A"):
        reasons.append(f"SAR {info.sar}")
    if info.faststart is False:
        reasons.append("moov atom at the end")
    return reasons


def target_box(info: MediaInfo, short: int) -> tuple[int, int]:
    long = round(short * 16 / 9 / 2) * 2
    if info.width == info.height:
        return short, short
    return (short, long) if info.height > info.width else (long, short)


def output_fps(info: MediaInfo, settings: EncodeSettings) -> float:
    """Constant output frame rate: the source rate, capped at max_fps (30)."""
    if not info.fps or info.fps > settings.max_fps + 0.5:
        return float(settings.max_fps)
    return info.fps


def video_filter(info: MediaInfo, settings: EncodeSettings) -> tuple[str, tuple[int, int] | None]:
    fps = f"fps={output_fps(info, settings):g}"
    if info.short_side < settings.min_short_side:
        w, h = target_box(info, settings.min_short_side)
        return (f"scale={w}:{h}:force_original_aspect_ratio=decrease:flags=lanczos,"
                f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,{fps},format=yuv420p"), (w, h)
    return f"scale=trunc(iw/2)*2:trunc(ih/2)*2:flags=lanczos,setsar=1,{fps},format=yuv420p", None


def prepare_for_upload(src: Path, out_dir: Path, settings: EncodeSettings | None = None) -> tuple[Path, MediaInfo, MediaInfo]:
    """Return (path to upload, original info, uploaded info). Transcodes only when needed."""
    settings = settings or EncodeSettings()
    before = probe(src)
    reasons = transcode_reasons(before, settings)
    if not reasons:
        log.info("media OK as-is: %s", before.describe())
        return src, before, before
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{src.stem}.prepared.mp4"
    vf, box = video_filter(before, settings)
    gop = max(1, round(output_fps(before, settings) * 2))     # keyframe every 2 s
    cmd = ["ffmpeg", "-v", "error", "-y", "-i", str(src), "-map", "0:v:0", "-map", "0:a:0?",
           "-vf", vf, "-fps_mode", "cfr",
           "-c:v", "libx264", "-preset", settings.preset, "-crf", str(settings.crf),
           "-profile:v", "high", "-level:v", "4.1", "-pix_fmt", "yuv420p",
           "-g", str(gop), "-keyint_min", str(gop), "-sc_threshold", "0",
           "-c:a", "aac", "-b:a", settings.audio_bitrate, "-ar", "48000", "-ac", "2",
           "-movflags", "+faststart", str(out)]
    log.info("transcoding (%s): %s%s", "; ".join(reasons), before.describe(),
             f" → upscale into {box[0]}x{box[1]}" if box else "")
    started = time.monotonic()
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=settings.timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        out.unlink(missing_ok=True)
        raise TranscodeError(f"ffmpeg timed out after {settings.timeout_seconds}s") from exc
    if result.returncode != 0:
        out.unlink(missing_ok=True)
        raise TranscodeError(f"ffmpeg failed: {result.stderr.strip()[-400:]}")
    after = probe(out)
    log.info("transcoded in %.0fs: %s → %s", time.monotonic() - started, before.describe(), after.describe())
    return out, before, after
