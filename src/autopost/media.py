"""Video inspection with ffprobe (orientation is computed after applying rotation metadata)."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
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

    @property
    def vertical(self) -> bool:
        return self.height > self.width

    def describe(self) -> str:
        return f"{self.width}x{self.height} {self.duration_s:.1f}s{' (rotated)' if self.rotation else ''}"


class ProbeError(Exception):
    pass


def ffprobe_available() -> bool:
    return shutil.which("ffprobe") is not None


def probe(path: str | Path) -> MediaInfo:
    if not ffprobe_available():
        raise ProbeError("ffprobe is not installed")
    cmd = ["ffprobe", "-v", "error", "-select_streams", "v:0",
           "-show_entries", "stream=width,height:stream_side_data=rotation:stream_tags=rotate:format=duration",
           "-of", "json", str(path)]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise ProbeError(f"ffprobe failed: {result.stderr.strip()[:300]}")
    data = json.loads(result.stdout or "{}")
    streams = data.get("streams") or []
    if not streams:
        raise ProbeError("no video stream")
    s = streams[0]
    width, height = int(s.get("width") or 0), int(s.get("height") or 0)
    rotation = 0
    for side in s.get("side_data_list") or []:
        if "rotation" in side:
            rotation = int(float(side["rotation"]))
    if not rotation and (s.get("tags") or {}).get("rotate"):
        rotation = int(float(s["tags"]["rotate"]))
    if abs(rotation) % 180 == 90:
        width, height = height, width
    try:
        duration = float((data.get("format") or {}).get("duration") or 0)
    except ValueError:
        duration = 0.0
    if not (width and height and duration):
        raise ProbeError(f"incomplete probe result: {width}x{height} {duration}s")
    return MediaInfo(width, height, duration, rotation)


def from_drive(width: int | None, height: int | None, duration_ms: int | None) -> MediaInfo | None:
    """Drive's videoMediaMetadata (may ignore rotation; only a fallback/preview)."""
    if not (width and height and duration_ms):
        return None
    return MediaInfo(int(width), int(height), int(duration_ms) / 1000, source="drive")


def shorts_problem(info: MediaInfo, require_vertical: bool, max_seconds: int) -> str | None:
    """Why this video cannot go out as a YouTube Short, or None if it can."""
    if require_vertical and not info.vertical:
        return f"not vertical ({info.width}x{info.height})"
    if info.duration_s > max_seconds:
        return f"longer than {max_seconds}s ({info.duration_s:.0f}s)"
    return None
