"""Content source interface: which video comes next and which text goes with it."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol


@dataclass
class VideoItem:
    id: str
    name: str
    created: datetime | None
    size: int | None = None
    width: int | None = None
    height: int | None = None
    duration_ms: int | None = None
    web_link: str | None = None
    local_path: Path | None = None


@dataclass
class TextChoice:
    variables: dict[str, str]
    row: int | None = None          # sheet row number (1-based), None for filename sources
    extra: dict = field(default_factory=dict)


class ContentSource(Protocol):
    def next_video(self, brand: str, stream: str) -> VideoItem | None: ...

    def remaining(self, brand: str, stream: str) -> int: ...

    def video_by_id(self, brand: str, stream: str, source_id: str) -> VideoItem | None: ...

    def pick_text(self, brand: str, stream: str, video: VideoItem) -> TextChoice: ...

    def commit_text(self, brand: str, choice: TextChoice, when: datetime) -> None: ...

    def fetch(self, video: VideoItem) -> Path: ...

    def release(self, video: VideoItem) -> None: ...


class PlaceholderContent:
    """Endless fake videos for dry runs before the Drive integration (phase 2) is configured."""

    def __init__(self) -> None:
        self._n = 0

    def next_video(self, brand: str, stream: str) -> VideoItem | None:
        self._n += 1
        return VideoItem(id=f"fake-{brand}-{stream}-{self._n}", name=f"{brand}-{stream}-video-{self._n}.mp4",
                         created=None, width=1080, height=1920, duration_ms=45_000)

    def remaining(self, brand: str, stream: str) -> int:
        return 999

    def video_by_id(self, brand: str, stream: str, source_id: str) -> VideoItem | None:
        return VideoItem(id=source_id, name=f"{source_id}.mp4", created=None)

    def pick_text(self, brand: str, stream: str, video: VideoItem) -> TextChoice:
        title = video.name.rsplit(".", 1)[0]
        return TextChoice(variables={"title": title, "caption": f"[caption for {title}]"})

    def commit_text(self, brand: str, choice: TextChoice, when: datetime) -> None:
        pass

    def fetch(self, video: VideoItem) -> Path:
        return Path("/dev/null")

    def release(self, video: VideoItem) -> None:
        pass
