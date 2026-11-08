"""Cover (thumbnail) from a random good frame of the video.

Instagram takes the cover as an offset in milliseconds; Buffer takes a public image URL (`thumbnailUrl`).
Both come from this one choice, made once per video, so every platform shows the same frame.
Black, white and blank frames (fades, title cards) are rejected.
"""

from __future__ import annotations

import logging
import random
import subprocess
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Cover:
    offset_ms: int
    image_path: Path


def _usable(path: Path) -> bool:
    """A frame with some brightness range: not black, not blown out, not a flat colour."""
    from PIL import Image, ImageStat

    try:
        with Image.open(path) as im:
            stat = ImageStat.Stat(im.convert("L").resize((64, 114)))
    except OSError:
        return False
    return 35 <= stat.mean[0] <= 225 and stat.stddev[0] >= 22


def _extract(video: Path, at_s: float, out: Path) -> bool:
    cmd = ["ffmpeg", "-v", "error", "-y", "-ss", f"{at_s:.3f}", "-i", str(video), "-frames:v", "1",
           "-q:v", "2", str(out)]
    try:
        return subprocess.run(cmd, capture_output=True, timeout=120).returncode == 0 and out.exists()
    except (subprocess.TimeoutExpired, OSError):
        return False


def pick_cover(video: Path, duration_s: float, out_dir: Path, rng: random.Random | None = None,
               attempts: int = 8) -> Cover | None:
    """Random frame between 20% and 80% of the video (at least 1 s from either end). None if ffmpeg fails."""
    rng = rng or random.SystemRandom()
    lo, hi = max(1.0, duration_s * 0.2), min(duration_s - 1.0, duration_s * 0.8)
    if hi <= lo:
        lo, hi = duration_s * 0.3, duration_s * 0.7
    out = out_dir / f"{video.stem}.cover.jpg"
    fallback: float | None = None
    for _ in range(attempts):
        at = rng.uniform(lo, hi)
        if not _extract(video, at, out):
            continue
        if _usable(out):
            log.info("cover for %s: frame at %.1fs of %.1fs", video.name, at, duration_s)
            return Cover(round(at * 1000), out)
        fallback = at
    if fallback is not None and _extract(video, fallback, out):      # every candidate was dull: use the last one
        return Cover(round(fallback * 1000), out)
    out.unlink(missing_ok=True)
    return None
