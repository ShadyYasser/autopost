"""Content from Google Drive (videos) and Google Sheets (captions/titles).

Videos: video/* files directly inside each stream's folder, oldest Drive createdTime first,
skipping any already used by a post of that brand/stream (tracked in state).

Sheet texts rotate row by row, independently of the videos. The next row is the one after
the most recently posted row, judged by the bot-written date in the posted_at column (or the
cursor saved in state if that is newer); it wraps to the first data row after the last one.
"""

from __future__ import annotations

import dataclasses
import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from ..config import AppConfig, FilenameTextSource, SheetTextSource
from ..media import EncodeSettings, MediaInfo, ProbeError, TranscodeError, from_drive, prepare_for_upload, probe
from ..state import State
from .base import TextChoice, VideoItem
from .google_api import GoogleClient, GoogleError, a1_sheet, natural_key
from .text import title_from_filename

log = logging.getLogger(__name__)

POSTED_AT_FORMAT = "%Y-%m-%d %H:%M:%S"
_PARSE_FORMATS = (POSTED_AT_FORMAT, "%Y-%m-%d %H:%M", "%Y-%m-%d", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y")


class ContentError(Exception):
    pass


@dataclass
class SheetRow:
    number: int                   # 1-based row number in the sheet
    values: dict[str, str]
    posted_at: datetime | None


def column_index(letter: str) -> int:
    n = 0
    for ch in letter:
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def column_letter(index: int) -> str:
    out = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        out = chr(65 + rem) + out
    return out


def parse_posted_at(value: str, tz: ZoneInfo) -> datetime | None:
    value = (value or "").strip()
    if not value:
        return None
    for fmt in _PARSE_FORMATS:
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=tz)
        except ValueError:
            continue
    return None


def parse_rows(raw: list[list[str]], src: SheetTextSource, tz: ZoneInfo) -> list[SheetRow]:
    first = src.header_rows + 1
    posted_idx = column_index(src.posted_at_column)
    rows = []
    for offset, cells in enumerate(raw):
        def cell(i: int) -> str:
            return cells[i].strip() if i < len(cells) and cells[i] is not None else ""
        values = {name: cell(column_index(col)) for name, col in src.columns.items()}
        rows.append(SheetRow(first + offset, values, parse_posted_at(cell(posted_idx), tz)))
    return rows


_NUMBER = re.compile(r"\d+")


def name_number(name: str) -> int | None:
    """The first number in the file name (without extension), e.g. 68 for "68.mp4"."""
    m = _NUMBER.search(Path(name).stem)
    return int(m.group()) if m else None


def order_by_name_number(items: list[VideoItem]) -> tuple[list[VideoItem], list[VideoItem]]:
    """Numbered files by number (ties by full name), then unnumbered ones oldest-created first.
    Returns (ordered, unnumbered)."""
    numbered = [v for v in items if name_number(v.name) is not None]
    unnumbered = [v for v in items if name_number(v.name) is None]
    numbered.sort(key=lambda v: (name_number(v.name), natural_key(v.name), v.id))
    unnumbered.sort(key=lambda v: (v.created or datetime.max.astimezone(), natural_key(v.name), v.id))
    return numbered + unnumbered, unnumbered


def numbering_report(items: list[VideoItem]) -> dict:
    """Numbers in the file names: range, missing numbers (as ranges), duplicates, unnumbered names."""
    by_num: dict[int, list[str]] = {}
    unnumbered = []
    for v in items:
        n = name_number(v.name)
        if n is None:
            unnumbered.append(v.name)
        else:
            by_num.setdefault(n, []).append(v.name)
    nums = sorted(by_num)
    gaps: list[tuple[int, int]] = []
    for a, b in zip(nums, nums[1:]):
        if b > a + 1:
            gaps.append((a + 1, b - 1))
    return {
        "first": nums[0] if nums else None,
        "last": nums[-1] if nums else None,
        "count": len(nums),
        "gaps": gaps,
        "duplicates": {n: sorted(names) for n, names in by_num.items() if len(names) > 1},
        "unnumbered": sorted(unnumbered),
    }


def format_ranges(ranges: list[tuple[int, int]]) -> str:
    return ", ".join(str(a) if a == b else f"{a}-{b}" for a, b in ranges)


def choose_next_row(rows: list[SheetRow], cursor: tuple[int, datetime] | None = None) -> SheetRow:
    usable = [r for r in rows if all(r.values.values())]
    if not usable:
        raise ContentError("the sheet has no row with all text columns filled")
    last_num, last_when = None, None
    for r in rows:
        if r.posted_at and (last_when is None or (r.posted_at, r.number) > (last_when, last_num)):
            last_num, last_when = r.number, r.posted_at
    if cursor and (last_when is None or cursor[1] > last_when):
        last_num = cursor[0]
    if last_num is None:
        return usable[0]
    following = [r for r in usable if r.number > last_num]
    return following[0] if following else usable[0]


class DriveContent:
    def __init__(self, app: AppConfig, state: State, client: GoogleClient, work_dir: Path, dry_run: bool,
                 notify: Callable[[str, str], None] | None = None):
        self.app = app
        self.state = state
        self.client = client
        self.work_dir = work_dir
        self.dry_run = dry_run
        self.tz = app.settings.tz
        self._videos: dict[tuple[str, str], list[VideoItem]] = {}
        self._rows: dict[str, list[SheetRow]] = {}
        self._tabs: dict[str, str] = {}
        self.notify = notify or (lambda text, key: log.warning("%s", text))
        m = app.settings.media
        self.encode = EncodeSettings(min_short_side=m.min_short_side, crf=m.crf, preset=m.preset,
                                     audio_bitrate=m.audio_bitrate, timeout_seconds=m.transcode_timeout_minutes * 60,
                                     max_fps=m.max_fps)
        # video id -> (prepared path, its media info, files to delete on release)
        self._prepared: dict[str, tuple[Path, MediaInfo, list[Path]]] = {}

    # ------------------------------------------------------------ videos

    def listing(self, brand: str, stream: str) -> list[VideoItem]:
        key = (brand, stream)
        if key not in self._videos:
            folder = self.app.brands[brand].streams[stream].drive_folder_id
            try:
                items = self.client.list_videos(folder)
            except GoogleError as exc:
                raise ContentError(f"cannot list Drive folder {folder} ({brand}/{stream}): {exc}") from exc
            if self.app.brands[brand].streams[stream].order == "name_number":
                items, unnumbered = order_by_name_number(items)
                if unnumbered:
                    names = sorted(v.name for v in unnumbered)
                    digest = hashlib.sha1("|".join(sorted(v.id for v in unnumbered)).encode()).hexdigest()[:12]
                    self.notify(f"{self.app.brands[brand].name}/{stream}: {len(names)} file(s) without a number in "
                                f"the name will be posted after the numbered ones: " + ", ".join(names[:20]),
                                f"no-number:{brand}:{stream}:{digest}")
            self._videos[key] = items
            log.info("%s/%s: %d video(s) in Drive folder", brand, stream, len(self._videos[key]))
        return self._videos[key]

    def unused(self, brand: str, stream: str) -> list[VideoItem]:
        used = self.state.used_source_ids(brand, stream)
        return [v for v in self.listing(brand, stream) if v.id not in used]

    def next_video(self, brand: str, stream: str) -> VideoItem | None:
        pending = self.unused(brand, stream)
        return dataclasses.replace(pending[0]) if pending else None

    def remaining(self, brand: str, stream: str) -> int:
        return len(self.unused(brand, stream))

    def video_by_id(self, brand: str, stream: str, source_id: str) -> VideoItem | None:
        for v in self.listing(brand, stream):
            if v.id == source_id:
                return dataclasses.replace(v)
        try:
            return self.client.get_video(source_id)
        except GoogleError as exc:
            log.error("video %s not found: %s", source_id, exc)
            return None

    def fetch(self, video: VideoItem) -> Path:
        """Download once and prepare (transcode if needed); later calls reuse the prepared file."""
        cached = self._prepared.get(video.id)
        if cached and cached[0].exists():
            return cached[0]
        suffix = Path(video.name).suffix.lower() or ".mp4"
        dest = self.work_dir / f"{video.id}{suffix}"
        log.info("downloading %s (%s)", video.name, _size(video.size))
        try:
            original = self.client.download(video.id, dest, video.size)
            prepared, before, after = prepare_for_upload(original, self.work_dir, self.encode)
        except (GoogleError, ProbeError, TranscodeError) as exc:
            dest.unlink(missing_ok=True)
            raise ContentError(f"{video.name}: {exc}") from exc
        log.info("media for %s: original %s | upload %s", video.name, before.describe(), after.describe())
        self._prepared[video.id] = (prepared, after, [original, prepared])
        return prepared

    def release(self, video: VideoItem) -> None:
        _, _, files = self._prepared.pop(video.id, (None, None, []))
        for f in {*files, *([Path(video.local_path)] if video.local_path else [])}:
            if f.exists():
                f.unlink()
        if files or video.local_path:
            log.info("deleted local copies of %s", video.name)
        video.local_path = None

    def probe(self, video: VideoItem) -> MediaInfo | None:
        cached = self._prepared.get(video.id)
        if cached:
            return cached[1]
        if video.local_path:
            try:
                return probe(video.local_path)
            except ProbeError as exc:
                log.warning("ffprobe failed for %s (%s); using Drive metadata", video.name, exc)
        return from_drive(video.width, video.height, video.duration_ms)

    # ------------------------------------------------------------ texts

    def pick_text(self, brand: str, stream: str, video: VideoItem) -> TextChoice:
        src = self.app.brands[brand].text_source
        if isinstance(src, FilenameTextSource):
            title = title_from_filename(video.name, src.strip_prefixes, src.replacements)
            if not title:
                raise ContentError(f"empty title from file name {video.name!r}")
            return TextChoice(variables={"title": title})
        row = choose_next_row(self.sheet_rows(brand), self.state.caption_cursor(brand))
        return TextChoice(variables=dict(row.values), row=row.number)

    def sheet_rows(self, brand: str) -> list[SheetRow]:
        if brand not in self._rows:
            src = self._sheet_source(brand)
            last_col = column_letter(max(column_index(c) for c in [*src.columns.values(), src.posted_at_column]))
            try:
                tab = self.sheet_tab(brand)
                raw = self.client.read_range(src.sheet_id, f"{tab}!A{src.header_rows + 1}:{last_col}")
            except GoogleError as exc:
                raise ContentError(f"cannot read the {brand} sheet: {exc}") from exc
            rows = parse_rows(raw, src, self.tz)
            incomplete = [r.number for r in rows if not all(r.values.values())]
            if incomplete:
                log.warning("%s sheet: rows with an empty text cell are skipped: %s", brand, incomplete[:20])
            self._rows[brand] = rows
            log.info("%s sheet: %d data row(s)", brand, len(rows))
        return self._rows[brand]

    def sheet_tab(self, brand: str) -> str:
        if brand not in self._tabs:
            src = self._sheet_source(brand)
            self._tabs[brand] = a1_sheet(self.client.sheet_title(src.sheet_id, src.worksheet_index))
        return self._tabs[brand]

    def commit_text(self, brand: str, choice: TextChoice, when: datetime) -> None:
        if choice.row is None:
            return
        src = self._sheet_source(brand)
        stamp = when.astimezone(self.tz).strftime(POSTED_AT_FORMAT)
        for r in self._rows.get(brand, []):
            if r.number == choice.row:
                r.posted_at = when.astimezone(self.tz).replace(microsecond=0)
        self.state.set_caption_cursor(brand, choice.row, when)
        if self.dry_run:
            log.info("[dry-run] would write %s to %s row %d", stamp, src.posted_at_column, choice.row)
            return
        try:
            self.client.write_cell(src.sheet_id, f"{self.sheet_tab(brand)}!{src.posted_at_column}{choice.row}", stamp)
        except GoogleError as exc:
            raise ContentError(f"could not write the posted date to sheet row {choice.row}: {exc}") from exc

    def _sheet_source(self, brand: str) -> SheetTextSource:
        src = self.app.brands[brand].text_source
        if not isinstance(src, SheetTextSource):
            raise ContentError(f"{brand} does not use a sheet")
        return src


def _size(n: int | None) -> str:
    return "?" if n is None else f"{n / 1_048_576:.1f} MB"
