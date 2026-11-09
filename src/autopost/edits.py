"""Edits made from the reel's own picture and sound, so the upload is not a plain copy of the source.

Two edits, both cut from the video itself (no text is ever added to the picture):

- cold open: one complete sentence of the speaker, taken from later in the reel, plays first; then the reel
  starts from its beginning. The sentence is chosen by the language model among Whisper's segments.
- zoom cuts: the picture switches between its normal size and a light punch-in where the speaker starts a new
  sentence (Whisper's segment starts), never more often than `min_shot_seconds`.

Everything here is plain arithmetic on timestamps; ffmpeg does the work in `frame.render_video`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    text: str


def segments_to_json(segments: list[Segment]) -> str:
    return json.dumps([[round(s.start, 2), round(s.end, 2), s.text] for s in segments], ensure_ascii=False)


def segments_from_json(raw: str) -> list[Segment]:
    return [Segment(float(a), float(b), str(t)) for a, b, t in json.loads(raw)]


@dataclass(frozen=True)
class HookRules:
    min_seconds: float = 1.5
    max_seconds: float = 6.0
    skip_first_seconds: float = 8.0     # a sentence from the very start would simply be said twice in a row
    min_words: int = 4
    pad_before: float = 0.08            # Whisper's word times sit a little inside the word
    pad_after: float = 0.15


def hook_candidates(segments: list[Segment], rules: HookRules) -> list[Segment]:
    """Segments short enough for an opening line and far enough from the start, in the order they are said."""
    out = []
    for s in segments:
        length = s.end - s.start
        if (s.start >= rules.skip_first_seconds and rules.min_seconds <= length <= rules.max_seconds
                and len(s.text.split()) >= rules.min_words):
            out.append(s)
    return out


def hook_window(segment: Segment, rules: HookRules, duration: float) -> tuple[float, float]:
    """(start, end) of the cut in the source, with a little air around the words."""
    return max(0.0, segment.start - rules.pad_before), min(duration, segment.end + rules.pad_after)


def zoom_windows(segments: list[Segment], duration: float, min_shot: float = 4.0) -> list[tuple[float, float]]:
    """(start, end) spans of the source during which the picture is punched in.

    Cuts fall on segment starts at least `min_shot` apart; shots alternate normal / zoomed, starting normal.
    A zoomed shot that would run to the end of the reel stops at the last word instead."""
    cuts: list[float] = []
    last = 0.0
    for s in segments:
        if s.start - last >= min_shot and duration - s.start >= min_shot / 2:
            cuts.append(round(s.start, 2))
            last = s.start
    spoken_end = min(duration, max((s.end for s in segments), default=0.0))
    windows = []
    for i in range(0, len(cuts), 2):
        end = cuts[i + 1] if i + 1 < len(cuts) else spoken_end
        if end - cuts[i] >= 1.0:
            windows.append((cuts[i], round(end, 2)))
    return windows


@dataclass(frozen=True)
class OverlaySpec:
    """A decorative clip blended over the picture: black pixels of a `screen` overlay leave the reel untouched."""
    path: Path
    opacity: float = 0.1
    blend: str = "screen"
    protect_center: float = 0.45     # fraction of the picture kept completely free of the effect


@dataclass(frozen=True)
class OutroSpec:
    """A closing clip (a logo animation) played after the reel, sped up when it would not otherwise fit."""
    path: Path
    duration: float
    speed: float = 1.0

    @property
    def seconds(self) -> float:
        return self.duration / self.speed


@dataclass(frozen=True)
class ProgressStyle:
    """The ring that closes around the video window as the reel ends."""
    color: str = "#ffd98a"
    width: int = 9
    glow: int = 6


@dataclass(frozen=True)
class EditPlan:
    hook: tuple[float, float] | None = None                       # cut of the source that plays first
    zoom: list[tuple[float, float]] = field(default_factory=list)  # source spans shown punched in
    zoom_factor: float = 1.07
    overlay: OverlaySpec | None = None
    progress: ProgressStyle | None = None
    outro: OutroSpec | None = None

    def __bool__(self) -> bool:
        return (self.hook is not None or bool(self.zoom) or self.overlay is not None
                or self.progress is not None or self.outro is not None)

    @property
    def hook_seconds(self) -> float:
        return (self.hook[1] - self.hook[0]) if self.hook else 0.0

    @property
    def outro_seconds(self) -> float:
        return self.outro.seconds if self.outro else 0.0
