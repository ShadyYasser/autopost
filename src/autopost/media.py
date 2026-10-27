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
           "stream=codec_type,codec_name,pix_fmt,width,height:stream_side_data=rotation:stream_tags=rotate"
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
                     int(fmt["size"]) if fmt.get("size") else None)


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
    return reasons


def target_box(info: MediaInfo, short: int) -> tuple[int, int]:
    long = round(short * 16 / 9 / 2) * 2
    if info.width == info.height:
        return short, short
    return (short, long) if info.height > info.width else (long, short)


def video_filter(info: MediaInfo, settings: EncodeSettings) -> tuple[str, tuple[int, int] | None]:
    if info.short_side < settings.min_short_side:
        w, h = target_box(info, settings.min_short_side)
        return (f"scale={w}:{h}:force_original_aspect_ratio=decrease:flags=lanczos,"
                f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,format=yuv420p"), (w, h)
    return "scale=trunc(iw/2)*2:trunc(ih/2)*2,setsar=1,format=yuv420p", None


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
    cmd = ["ffmpeg", "-v", "error", "-y", "-i", str(src), "-map", "0:v:0", "-map", "0:a:0?",
           "-vf", vf, "-c:v", "libx264", "-preset", settings.preset, "-crf", str(settings.crf),
           "-profile:v", "high", "-c:a", "aac", "-b:a", settings.audio_bitrate, "-ac", "2",
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
