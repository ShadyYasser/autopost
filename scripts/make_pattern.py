#!/usr/bin/env python3
"""Generate the animated Islamic-pattern background for the cream margins of the frame.

Every motif is built from geometry, not traced from an image: an n-fold rosette is m = n/2 long "strips"
(two parallel lines closed at both ends by a pointed cap), laid at equal angles across one centre. Where the
strips cross they draw the interlace and the petals; inside the star they make in the middle the lines are
cleared.

NO CROSS, BY CONSTRUCTION. The rosettes are 10-fold (m = 5 strips, 36 degrees apart) and every cap is cut
at exactly 36 degrees to its strip, so every single line in the pattern runs at a multiple of 36 degrees.
90 is not a multiple of 36, so no two lines are ever perpendicular - not inside one rosette, not between
two, and not at any moment of the rotation. A "+" or a cross shape cannot form. (An 8- or 12-fold rosette
has a vertical and a horizontal strip, i.e. a "+", which is why they are not used.) tests/test_pattern.py
checks this.

The rosettes sit on a lattice whose columns fall in the middle of the side margins of a 9:16 reel. Two
proportions alternate like a chessboard and turn in opposite directions; a soft diagonal band of light
sweeps across once per loop. Every motion closes exactly at the end of the clip (a 10-fold rosette turns
36 degrees), so the clip loops without a jump. It is white on black: the publisher colours it gold, keeps
it off the video window, the logo and the credit line, and sets its opacity.

    python scripts/make_pattern.py                 # rewrite assets/kalima_sawaa/pattern.mp4
    python scripts/make_pattern.py --preview p.png # also save a still of the first frame
"""

from __future__ import annotations

import argparse
import math
import subprocess
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parents[1]
W, H, FPS = 1080, 1920, 30
SPACING = 121                      # columns at x = 56 + 121k: the middle of both side margins of a 9:16 reel
ORIGIN = (56, 70)                  # first row in the top bar, clear of the window (which starts at y=270)
RADIUS = 47
SUPERSAMPLE = 3

STRIPS = 5                         # 10-fold; must be odd, see "NO CROSS" above
# two proportions (strip half-width as a fraction of the radius); the cap length follows from the 36 degree cut
WIDE, SLIM = 0.21, 0.15


def cap_length(w: float, strips: int = STRIPS) -> float:
    """Length of a cap whose two sides meet the strip at exactly 180/strips degrees."""
    return w / math.tan(math.pi / strips)


def rosette_lines(w_f: float, radius: float, angle: float, strips: int = STRIPS):
    """Every line segment of one rosette, as ((x0, y0), (x1, y1)) around 0,0."""
    R, w = radius, w_f * radius
    cap = cap_length(w, strips)
    segs = []
    for k in range(strips):
        t = angle + math.pi * k / strips - math.pi / 2
        ux, uy, px, py = math.cos(t), math.sin(t), -math.sin(t), math.cos(t)

        def P(across, along):
            return (across * px + along * ux, across * py + along * uy)

        ring = [P(-w, R - cap), P(0, R), P(w, R - cap), P(w, -(R - cap)), P(0, -R), P(-w, -(R - cap)),
                P(-w, R - cap)]
        segs += list(zip(ring, ring[1:]))
    return segs


def centre_star(w: float, angle: float, strips: int = STRIPS):
    """Corners of the parallelograms where strip k crosses strip k + strips//2; their union is the star in
    the middle, whose inside is cleared."""
    shapes = []
    for k in range(strips):
        normals = []
        for j in (k, (k + strips // 2) % strips):
            t = angle + math.pi * j / strips - math.pi / 2
            normals.append((-math.sin(t), math.cos(t)))
        (a, b), (c, d) = normals
        det = a * d - b * c
        corners = []
        for s1, s2 in ((1, 1), (-1, 1), (-1, -1), (1, -1)):
            r1, r2 = s1 * w, s2 * w
            corners.append(((r1 * d - b * r2) / det, (a * r2 - c * r1) / det))
        shapes.append(corners)
    return shapes


def rosette(w_f: float, radius: float, angle: float, width: float) -> np.ndarray:
    """One rosette as a float alpha sprite (0..1), centred, turned by `angle` radians."""
    size = int(math.ceil(radius * 2 + width * 4)) | 1
    s = SUPERSAMPLE
    c = size * s / 2
    w = w_f * radius
    img = Image.new("L", (size * s, size * s), 0)
    d = ImageDraw.Draw(img)
    for (x0, y0), (x1, y1) in rosette_lines(w_f, radius, angle):
        d.line([(c + x0 * s, c + y0 * s), (c + x1 * s, c + y1 * s)], fill=255, width=max(1, round(width * s)))
    # round joints: a dot at every corner, so the lines meet cleanly
    r = width * s / 2
    for (x0, y0), _ in rosette_lines(w_f, radius, angle):
        d.ellipse([c + x0 * s - r, c + y0 * s - r, c + x0 * s + r, c + y0 * s + r], fill=255)
    star = Image.new("L", img.size, 0)
    sd = ImageDraw.Draw(star)
    for corners in centre_star(w, angle):
        sd.polygon([(c + x * s, c + y * s) for x, y in corners], fill=255)
    inner = star.filter(ImageFilter.MinFilter((round(width * s) + 1) | 1))
    a = np.asarray(img, dtype=np.float32) * (1 - np.asarray(inner, dtype=np.float32) / 255)
    small = Image.fromarray(a.astype(np.uint8)).resize((size, size), Image.LANCZOS)
    return np.asarray(small, dtype=np.float32) / 255


def lattice():
    """(x, y, kind) of every rosette: kind 0 = wide, 1 = slim, alternating like a chessboard."""
    out = []
    for row, y in enumerate(range(ORIGIN[1], H + SPACING, SPACING)):
        for col, x in enumerate(range(ORIGIN[0], W + SPACING, SPACING)):
            out.append((x, y, (row + col) % 2))
    return out


def paste(canvas: np.ndarray, sprite: np.ndarray, cx: int, cy: int) -> None:
    h, w = sprite.shape
    x0, y0 = cx - w // 2, cy - h // 2
    xa, ya, xb, yb = max(0, x0), max(0, y0), min(W, x0 + w), min(H, y0 + h)
    if xa >= xb or ya >= yb:
        return
    region = canvas[ya:yb, xa:xb]
    np.maximum(region, sprite[ya - y0:yb - y0, xa - x0:xb - x0], out=region)


def frames(seconds: int, width: float):
    total = seconds * FPS
    spots = lattice()
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    diag = (xx + yy) / (W + H)                                   # 0 at the top-left corner, 1 at the bottom-right
    for f in range(total):
        u = f / total
        sprites = {}
        turn = math.pi / STRIPS                                   # one symmetry step per loop: 36 degrees
        for kind, wf in enumerate((WIDE, SLIM)):
            for direction in (1, -1):
                sprites[kind, direction] = rosette(wf, RADIUS, direction * turn * u, width)
        canvas = np.zeros((H, W), dtype=np.float32)
        for x, y, kind in spots:
            direction = 1 if ((x - ORIGIN[0]) // SPACING) % 2 == 0 else -1
            paste(canvas, sprites[kind, direction], x, y)
        # a soft band of light sweeping across: the lines rest at 55% and glow to full as it passes
        d = (diag - (u * 1.6 - 0.3)) / 0.16
        canvas *= 0.55 + 0.45 * np.exp(-d * d)
        yield (np.clip(canvas, 0, 1) * 255).astype(np.uint8)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="assets/kalima_sawaa/pattern.mp4")
    ap.add_argument("--seconds", type=int, default=12, help="length of one loop")
    ap.add_argument("--width", type=float, default=2.2, help="line width in pixels at 1080x1920")
    ap.add_argument("--preview", help="also save the first frame here")
    args = ap.parse_args(argv)
    out = Path(args.out)
    out = out if out.is_absolute() else ROOT / out
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "gray", "-s", f"{W}x{H}",
           "-framerate", str(FPS), "-i", "-", "-an", "-c:v", "libx264", "-preset", "slow", "-crf", "28",
           "-pix_fmt", "yuv420p", "-g", str(FPS), "-movflags", "+faststart", str(out)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    for i, frame in enumerate(frames(args.seconds, args.width)):
        if i == 0 and args.preview:
            Image.fromarray(frame).save(args.preview)
        proc.stdin.write(frame.tobytes())
    proc.stdin.close()
    if proc.wait() != 0:
        raise SystemExit("ffmpeg failed")
    print(f"{out.name}: {out.stat().st_size // 1024} KB, {args.seconds}s loop")
    return 0


if __name__ == "__main__":
    sys.exit(main())
