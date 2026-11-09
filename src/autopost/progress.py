"""The progress ring: a glowing line that travels around the video window and closes as the reel ends.

The ring follows the same rounded-rectangle path as the frame's gold border. Its frames are drawn once per
video as grayscale masks (the shape never changes, only how much of it is lit), written as a numbered PNG
sequence and fed to ffmpeg as one more input: `alphamerge` turns each mask into the coloured ring, and the
sequence is played at exactly `steps / output duration` frames per second, so the lap closes with the reel.
"""

from __future__ import annotations

import logging
import math
import shutil
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

log = logging.getLogger(__name__)

SUPERSAMPLE = 2
PATTERN = "ring%05d.png"
STEPS_PER_SECOND = 10
MIN_STEPS, MAX_STEPS = 90, 600


@dataclass(frozen=True)
class ProgressSpec:
    """A rendered ring sequence: where its frames are and how fast they play."""
    pattern: Path
    steps: int
    fps: Fraction
    color: str
    offset: tuple[int, int]          # where the sequence is overlaid on the 1080x1920 frame
    size: tuple[int, int]


def step_count(duration: float) -> int:
    return max(MIN_STEPS, min(MAX_STEPS, round(duration * STEPS_PER_SECOND)))


def ring_alpha(size: tuple[int, int], inset: float, radius: int, width: int, glow: int):
    """Alpha of the whole ring (numpy float array, 0..1): a sharp line with a soft glow around it.

    `inset` is the distance from the image edge to the middle of the line, so the glow has room on both sides."""
    import numpy as np
    from PIL import Image, ImageDraw, ImageFilter

    w, h = size
    s = SUPERSAMPLE
    big = Image.new("L", (w * s, h * s), 0)
    mid = inset * s
    ImageDraw.Draw(big).rounded_rectangle([mid, mid, w * s - mid - 1, h * s - mid - 1],
                                          radius=radius * s, outline=255, width=max(1, round(width * s)))
    core = big.resize((w, h), Image.LANCZOS)
    halo = core.filter(ImageFilter.GaussianBlur(glow)) if glow > 0 else core
    core_a = np.asarray(core, dtype=np.float32) / 255.0
    halo_a = np.asarray(halo, dtype=np.float32) / 255.0
    return np.clip(core_a + halo_a * 0.75, 0.0, 1.0)


def path_points(size: tuple[int, int], inset: float, radius: int, per_quarter: int = 48):
    """The rounded-rectangle path, clockwise from the top middle, as a list of points."""
    w, h = size
    left, top, right, bottom = inset, inset, w - inset, h - inset
    r = min(radius, (right - left) / 2, (bottom - top) / 2)
    cx = (left + right) / 2
    pts: list[tuple[float, float]] = []

    def arc(ox, oy, a0, a1):
        for i in range(per_quarter + 1):
            a = math.radians(a0 + (a1 - a0) * i / per_quarter)
            pts.append((ox + r * math.cos(a), oy + r * math.sin(a)))

    pts.append((cx, top))
    pts.append((right - r, top))
    arc(right - r, top + r, -90, 0)
    pts.append((right, bottom - r))
    arc(right - r, bottom - r, 0, 90)
    pts.append((left + r, bottom))
    arc(left + r, bottom - r, 90, 180)
    pts.append((left, top + r))
    arc(left + r, top + r, 180, 270)
    pts.append((cx, top))
    return pts


def sweep_angles(size: tuple[int, int], inset: float, radius: int, steps: int) -> list[float]:
    """The pieslice end angle of each step, so the lit point moves at a steady speed along the path.

    A wedge grown by equal angles would race across the short edges and crawl down the long ones, so each
    step's angle is the one whose point lies at that fraction of the path's LENGTH."""
    pts = path_points(size, inset, radius)
    cx, cy = size[0] / 2, size[1] / 2
    lengths = [0.0]
    for (ax, ay), (bx, by) in zip(pts, pts[1:]):
        lengths.append(lengths[-1] + math.hypot(bx - ax, by - ay))
    total = lengths[-1]
    angles, j = [], 0
    for i in range(1, steps + 1):
        target = total * i / steps
        while j + 2 < len(lengths) and lengths[j + 1] < target:
            j += 1
        # the long sides are two points apart, so take the exact spot along the segment, not its end
        span = lengths[j + 1] - lengths[j]
        f = min(1.0, max(0.0, (target - lengths[j]) / span)) if span else 1.0
        (ax, ay), (bx, by) = pts[j], pts[j + 1]
        x, y = ax + (bx - ax) * f, ay + (by - ay) * f
        turns = math.degrees(math.atan2(y - cy, x - cx)) + 90        # 0 at the top, growing clockwise
        angles.append(-90 + (turns % 360 or (360 if i == steps else 0)))
    angles[-1] = 270.0
    return angles


def lit_mask(size: tuple[int, int], end_angle: float):
    """Alpha (0..1) of the wedge from the centre between the top of the frame and `end_angle`."""
    import numpy as np
    from PIL import Image, ImageDraw

    w, h = size
    if end_angle <= -90:
        return np.zeros((h, w), dtype=np.float32)
    if end_angle >= 270:
        return np.ones((h, w), dtype=np.float32)
    cx, cy = w / 2, h / 2
    r = math.hypot(w, h)
    wedge = Image.new("L", (w, h), 0)
    ImageDraw.Draw(wedge).pieslice([cx - r, cy - r, cx + r, cy + r], -90, end_angle, fill=255)
    return np.asarray(wedge, dtype=np.float32) / 255.0


def render_sequence(out_dir: Path, size: tuple[int, int], inset: float, radius: int, width: int, glow: int,
                    steps: int, duration: float, color: str, offset: tuple[int, int] = (0, 0)) -> ProgressSpec:
    """Write `steps` mask frames into a fresh directory and describe how to play them over `duration`."""
    import numpy as np
    from PIL import Image

    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    ring = ring_alpha(size, inset, radius, width, glow)
    for i, angle in enumerate(sweep_angles(size, inset, radius, steps)):
        alpha = ring * lit_mask(size, angle)
        Image.fromarray((alpha * 255).astype(np.uint8), mode="L").save(out_dir / (PATTERN % (i + 1)),
                                                                       optimize=False, compress_level=1)
    fps = Fraction(steps / max(duration, 0.1)).limit_denominator(10000)
    log.info("progress ring: %d frames at %s fps over %.1fs", steps, fps, duration)
    return ProgressSpec(out_dir / PATTERN, steps, fps, color, offset, size)
