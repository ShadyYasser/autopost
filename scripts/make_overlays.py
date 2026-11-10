#!/usr/bin/env python3
"""Generate the decorative overlay clips in assets/<brand>/overlays/ — no stock footage, no licences.

Each clip is particles drawn on black, so `screen` blending leaves the reel's own picture untouched where
the clip is dark, and every particle's position is periodic in the frame number, so a short clip loops
under a long reel without a visible jump.

Keeping the middle of the picture clear is NOT done here: the publisher masks the middle out of whatever
clip it is given (`edits.overlay.protect_center`), so a clip you drop in yourself is handled the same way.

    python scripts/make_overlays.py                     # rewrite the default clips
    python scripts/make_overlays.py --out other/folder --seconds 6
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
W, H, FPS = 540, 960, 30


@dataclass(frozen=True)
class Look:
    name: str
    count: int                      # particles
    radius: tuple[float, float]     # pixels, min/max
    speed: tuple[float, float]      # fraction of the height per second, min/max (negative = upward)
    drift: float                    # horizontal wander, pixels
    colors: tuple[tuple[int, int, int], ...]
    gain: tuple[float, float]       # brightness, min/max
    twinkle: float                  # how much the brightness pulses, 0..1


LOOKS = (
    Look("embers", 170, (1.8, 4.6), (-0.28, -0.09), 26, ((255, 150, 60), (255, 200, 110), (255, 110, 45)),
         (1.1, 2.2), 0.55),
    Look("light-leak", 7, (160.0, 340.0), (-0.05, 0.05), 90, ((255, 170, 95), (255, 205, 130), (190, 150, 255)),
         (0.5, 0.95), 0.25),
    Look("dust", 260, (1.0, 2.6), (-0.05, 0.05), 14, ((255, 232, 190), (235, 240, 255)), (0.8, 1.5), 0.7),
)


def splat(canvas: np.ndarray, cx: float, cy: float, radius: float, color, gain: float) -> None:
    """Add one soft round particle to an HxWx3 float canvas."""
    r = max(1, int(radius * 3))
    x0, x1 = max(0, int(cx) - r), min(W, int(cx) + r + 1)
    y0, y1 = max(0, int(cy) - r), min(H, int(cy) + r + 1)
    if x0 >= x1 or y0 >= y1:
        return
    yy = (np.arange(y0, y1, dtype=np.float32) - cy)[:, None]
    xx = (np.arange(x0, x1, dtype=np.float32) - cx)[None, :]
    falloff = np.exp(-(xx ** 2 + yy ** 2) / (2.0 * radius ** 2)) * gain
    canvas[y0:y1, x0:x1] += falloff[:, :, None] * np.asarray(color, dtype=np.float32)


def frames(look: Look, seconds: int, seed: int):
    """Yield `seconds * FPS` frames of one look as uint8 RGB arrays."""
    total = seconds * FPS
    rng = np.random.default_rng(seed)
    n = look.count
    x0 = rng.uniform(0, W, n).astype(np.float32)
    y0 = rng.uniform(0, H, n).astype(np.float32)
    radius = rng.uniform(*look.radius, n).astype(np.float32)
    gain = rng.uniform(*look.gain, n).astype(np.float32)
    color = np.asarray(look.colors, dtype=np.float32)[rng.integers(0, len(look.colors), n)]
    # one whole number of laps per clip, so the last frame runs back into the first
    laps = np.maximum(1, np.round(rng.uniform(*look.speed, n) * seconds)).astype(np.float32)
    phase = rng.uniform(0, 2 * np.pi, n).astype(np.float32)
    beats = rng.integers(1, 4, n).astype(np.float32)

    for f in range(total):
        u = f / total
        canvas = np.zeros((H, W, 3), dtype=np.float32)
        cy = (y0 + laps * u * H) % H
        cx = (x0 + np.sin(phase + 2 * np.pi * u) * look.drift) % W
        pulse = 1.0 - look.twinkle * 0.5 * (1.0 - np.cos(phase + 2 * np.pi * beats * u))
        for i in range(n):
            splat(canvas, float(cx[i]), float(cy[i]), float(radius[i]), color[i], float(gain[i] * pulse[i]))
        yield np.clip(canvas, 0, 255).astype(np.uint8)


def write_clip(look: Look, out: Path, seconds: int, seed: int) -> None:
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}",
           "-framerate", str(FPS), "-i", "-", "-an", "-c:v", "libx264", "-preset", "slow", "-crf", "28",
           "-pix_fmt", "yuv420p", "-g", str(FPS), "-movflags", "+faststart", str(out)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    for frame in frames(look, seconds, seed):
        proc.stdin.write(frame.tobytes())
    proc.stdin.close()
    if proc.wait() != 0:
        raise SystemExit(f"ffmpeg failed on {out.name}")
    print(f"{out.name}: {out.stat().st_size // 1024} KB")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="assets/kalima_sawaa/overlays", help="folder to write the clips into")
    ap.add_argument("--seconds", type=int, default=8, help="length of each loop")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--only", help="generate just this look")
    args = ap.parse_args(argv)
    folder = Path(args.out)
    folder = folder if folder.is_absolute() else ROOT / folder
    folder.mkdir(parents=True, exist_ok=True)
    for i, look in enumerate(LOOKS):
        if args.only and look.name != args.only:
            continue
        write_clip(look, folder / f"{look.name}.mp4", args.seconds, args.seed + i)
    return 0


if __name__ == "__main__":
    sys.exit(main())
