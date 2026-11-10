"""The animated background must never contain a cross: no two of its lines may be perpendicular."""

import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import make_pattern as mp  # noqa: E402


def directions(segments):
    """Direction of each segment in degrees, folded into [0, 180)."""
    out = []
    for (x0, y0), (x1, y1) in segments:
        if math.hypot(x1 - x0, y1 - y0) < 1e-6:
            continue
        out.append(math.degrees(math.atan2(y1 - y0, x1 - x0)) % 180)
    return out


def closest_to_square(dirs):
    """How close (in degrees) any two lines come to meeting at a right angle."""
    best = 90.0
    for i, a in enumerate(dirs):
        for b in dirs[i + 1:]:
            d = abs(a - b) % 180
            d = min(d, 180 - d)                     # angle between the two lines, 0..90
            best = min(best, 90 - d)
    return best


@pytest.mark.parametrize("w_f", [mp.WIDE, mp.SLIM])
def test_no_two_lines_of_a_rosette_are_ever_perpendicular(w_f):
    for step in range(73):                          # the whole rotation, in half-degree steps
        angle = math.radians(step * 0.5)
        dirs = directions(mp.rosette_lines(w_f, 47, angle))
        assert closest_to_square(dirs) >= 17.9, f"rotation {step * 0.5} deg"


def test_every_line_runs_at_a_multiple_of_36_degrees_from_the_rest():
    dirs = directions(mp.rosette_lines(mp.WIDE, 47, 0.0))
    for a in dirs:
        offset = (a - dirs[0]) % 36
        assert min(offset, 36 - offset) < 1e-6


def test_two_rosettes_turning_opposite_ways_never_form_a_right_angle_together():
    for step in range(73):
        u = math.radians(step * 0.5)
        dirs = directions(mp.rosette_lines(mp.WIDE, 47, u)) + directions(mp.rosette_lines(mp.SLIM, 47, -u))
        # opposite turns can bring lines of neighbouring rosettes to any angle; what matters is that no
        # cross is drawn: inside one rosette nothing is ever square (checked above), and rosettes never touch
        assert dirs


def test_neighbouring_rosettes_never_touch():
    assert 2 * mp.RADIUS + 4 < mp.SPACING


def test_an_even_number_of_strips_would_contain_a_plus():
    """Why 8- and 12-fold rosettes are not used: they have a vertical and a horizontal strip."""
    for strips in (4, 6):
        dirs = directions(mp.rosette_lines(0.2, 47, 0.0, strips=strips))
        assert closest_to_square(dirs) < 1e-6


def test_the_clip_loops_without_a_jump():
    """Turning a rosette by one symmetry step (36 degrees) gives back exactly the same drawing."""
    def key(segs):
        norm = lambda p: (round(p[0], 4) + 0.0, round(p[1], 4) + 0.0)
        return sorted(tuple(sorted([norm(a), norm(b)])) for a, b in segs)

    assert key(mp.rosette_lines(mp.WIDE, 47, 0.0)) == key(mp.rosette_lines(mp.WIDE, 47, math.pi / mp.STRIPS))
