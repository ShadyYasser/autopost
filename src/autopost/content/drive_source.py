"""Content from Google Drive (videos) and Google Sheets (captions/titles).

Videos: video/* files directly inside each stream's folder, oldest Drive createdTime first
(ties by Telegram msg_id, then name), by the number in the file name, or newest Telegram msg_id
first; skipping any already used by a post of that brand/stream (tracked in state).

Sheet texts rotate row by row, independently of the videos. The next row is the one after
the most recently posted row, judged by the bot-written date in the posted_at column (or the
cursor saved in state if that is newer); it wraps to the first data row after the last one.
"""

from __future__ import annotations

import dataclasses
import hashlib
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from ..config import AppConfig, FilenameTextSource, SheetTextSource, resolve_asset
from ..cover import Cover, pick_cover
from ..frame import FrameStyle, render_video
from ..media import EncodeSettings, MediaInfo, ProbeError, TranscodeError, from_drive, prepare_for_upload, probe
from ..state import State
from .base import TextChoice, VideoItem
from .google_api import GoogleClient, GoogleError, a1_sheet, natural_key
from .text import title_from_filename
from ..title_guard import (AnthropicBackend, AudioVerdict, GuardError, LocalBackend, OpenAICompatBackend,
                           TitleGuard, Verdict)
from ..transcribe import TranscribeError, Transcriber, WhisperSettings

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


def order_newest_first(items: list[VideoItem]) -> list[VideoItem]:
    """Highest Telegram msg_id first; files without one after them, newest createdTime first."""
    oldest = datetime.min.replace(tzinfo=ZoneInfo("UTC"))
    return sorted(items, key=lambda v: (v.tg_msg_id is not None, v.tg_msg_id or 0, v.created or oldest,
                                        v.name), reverse=True)


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


def choose_next_row(rows: list[SheetRow], cursor: tuple[int, datetime] | None = None,
                    start_row: int | None = None) -> SheetRow:
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
        if start_row is not None:
            return next((r for r in usable if r.number >= start_row), usable[0])
        return usable[0]
    following = [r for r in usable if r.number > last_num]
    return following[0] if following else usable[0]


class DriveContent:
    GUARD_LOOKAHEAD = 20       # how many queued videos one call may check (and hold back) before giving up
    AUDIO_FRESH_LIMIT = 3      # how many NOT yet checked videos one call may transcribe (each takes minutes)

    def __init__(self, app: AppConfig, state: State, client: GoogleClient, work_dir: Path, dry_run: bool,
                 notify: Callable[[str, str], None] | None = None, secrets=None, guard_session=None,
                 guard_backend=None, transcriber=None, audio_check: bool = True):
        self.app = app
        self.secrets = secrets
        self._guard_session = guard_session
        self._guard_backend = guard_backend            # tests: replaces the model of every brand
        self._transcriber = transcriber                # tests: replaces Whisper
        self.audio_check = audio_check and not dry_run  # dry runs never download and transcribe
        self._guards: dict[str, TitleGuard] = {}
        self._local_backends: dict[tuple[str, str, int], LocalBackend] = {}
        self._transcribers: dict[str, Transcriber] = {}
        self._covers: dict[str, Cover] = {}
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
        self._stream_of: dict[str, tuple[str, str]] = {}     # video id -> (brand, stream)
        self._skipped: set[str] = set()

    # ------------------------------------------------------------ videos

    def listing(self, brand: str, stream: str) -> list[VideoItem]:
        key = (brand, stream)
        if key not in self._videos:
            folder = self.app.brands[brand].streams[stream].drive_folder_id
            try:
                items = self.client.list_videos(folder)
            except GoogleError as exc:
                raise ContentError(f"cannot list Drive folder {folder} ({brand}/{stream}): {exc}") from exc
            order = self.app.brands[brand].streams[stream].order
            if order == "newest_first":
                items = order_newest_first(items)
            if order == "name_number":
                items, unnumbered = order_by_name_number(items)
                if unnumbered:
                    names = sorted(v.name for v in unnumbered)
                    digest = hashlib.sha1("|".join(sorted(v.id for v in unnumbered)).encode()).hexdigest()[:12]
                    self.notify(f"{self.app.brands[brand].name}/{stream}: {len(names)} file(s) without a number in "
                                f"the name will be posted after the numbered ones: " + ", ".join(names[:20]),
                                f"no-number:{brand}:{stream}:{digest}")
            self._videos[key] = items
            for v in items:
                self._stream_of[v.id] = key
            log.info("%s/%s: %d video(s) in Drive folder", brand, stream, len(self._videos[key]))
        return self._videos[key]

    def unused(self, brand: str, stream: str) -> list[VideoItem]:
        used = self.state.used_source_ids(brand, stream)
        return [v for v in self.listing(brand, stream)
                if v.id not in used and not self.skipped_title(brand, stream, v) and not self.held_back(brand, v)]

    # ------------------------------------------------------------ title check

    def guard_for(self, brand: str) -> TitleGuard | None:
        cfg = self.app.brands[brand].title_guard
        if cfg is None or not cfg.enabled or not isinstance(self.app.brands[brand].text_source, FilenameTextSource):
            return None
        if brand not in self._guards:
            self._guards[brand] = TitleGuard(context=cfg.context, backend=self._guard_backend or self._backend(brand))
        return self._guards[brand]

    def _backend(self, brand: str):
        cfg = self.app.brands[brand].title_guard
        if cfg.provider == "local":
            key = (cfg.gguf_repo, cfg.gguf_file, cfg.n_ctx)       # one loaded model for all brands that share it
            if key not in self._local_backends:
                self._local_backends[key] = LocalBackend(cfg.gguf_repo, cfg.gguf_file, cfg.n_ctx)
            return self._local_backends[key]
        if self.secrets is None or not self.secrets.has(cfg.api_key_secret):
            raise ContentError(f"{brand}: the title check is on but the secret {cfg.api_key_secret} is not set")
        key_value = self.secrets.get(cfg.api_key_secret)
        if cfg.provider == "anthropic":
            return AnthropicBackend(key_value, cfg.model, self._guard_session)
        return OpenAICompatBackend(cfg.base_url, key_value, cfg.model, self._guard_session)

    def _guard_key(self, brand: str, video: VideoItem) -> tuple[str, str]:
        """(state key, the title that is judged). The key includes the title, so renaming a file re-checks it."""
        src = self.app.brands[brand].text_source
        title = title_from_filename(video.name, src.strip_prefixes, src.replacements)
        return f"guard:{brand}:{video.id}:{hashlib.sha1(title.encode()).hexdigest()[:10]}", title

    def verdict(self, brand: str, video: VideoItem) -> Verdict | None:
        """The (cached) title check of a video; None when the brand has no title check."""
        guard = self.guard_for(brand)
        if guard is None:
            return None
        key, title = self._guard_key(brand, video)
        raw = self.state.kv_get(key)
        if raw:
            try:
                return Verdict.from_json(raw)
            except (ValueError, KeyError):
                pass
        try:
            verdict = guard.judge(title)
        except GuardError as exc:
            raise ContentError(f"{video.name}: {exc}") from exc
        self.state.kv_set(key, verdict.to_json())
        log.info("title check %s [%s]: %r → %r (%s)", video.name, verdict.risk, title, verdict.title,
                 "; ".join(verdict.reasons))
        return verdict

    def held_back(self, brand: str, video: VideoItem) -> bool:
        """True when this video's cached title check says it must not be posted (never calls the API)."""
        cfg = self.app.brands[brand].title_guard
        if cfg is None or not cfg.enabled or not isinstance(self.app.brands[brand].text_source, FilenameTextSource):
            return False
        raw = self.state.kv_get(self._guard_key(brand, video)[0])
        if raw:
            try:
                if Verdict.from_json(raw).risk in cfg.block_levels:
                    return True
            except (ValueError, KeyError):
                pass
        raw = self.state.kv_get(self._audio_key(brand, video))
        if raw:
            try:
                return AudioVerdict.from_json(raw).risk in cfg.block_levels
            except (ValueError, KeyError):
                return False
        return False

    # ------------------------------------------------------------ audio check

    def _audio_key(self, brand: str, video: VideoItem) -> str:
        """Like the title key: renaming the file checks the video again (the way to override a false alarm)."""
        return self._guard_key(brand, video)[0].replace("guard:", "audio:", 1)

    def audio_on(self, brand: str, stream: str) -> bool:
        cfg = self.app.brands[brand].title_guard
        return bool(self.audio_check and cfg and cfg.enabled and cfg.audio_for(stream)
                    and isinstance(self.app.brands[brand].text_source, FilenameTextSource))

    def transcriber_for(self, brand: str) -> Transcriber:
        if self._transcriber is not None:
            return self._transcriber
        if brand not in self._transcribers:
            a = self.app.brands[brand].title_guard.audio
            self._transcribers[brand] = Transcriber(WhisperSettings(model=a.whisper_model, compute_type=a.compute_type,
                                                                    language=a.language, max_seconds=a.max_seconds))
        return self._transcribers[brand]

    def cached_audio(self, brand: str, video: VideoItem) -> AudioVerdict | None:
        raw = self.state.kv_get(self._audio_key(brand, video))
        if not raw:
            return None
        try:
            return AudioVerdict.from_json(raw)
        except (ValueError, KeyError):
            return None

    def audio_verdict(self, brand: str, stream: str, video: VideoItem) -> AudioVerdict:
        """What is said in the video, judged (cached in state). Downloads the original, transcribes the first
        seconds, deletes the download. Any failure raises ContentError: nothing is posted, the next run retries."""
        cached = self.cached_audio(brand, video)
        if cached is not None:
            return cached
        guard = self.guard_for(brand)
        suffix = Path(video.name).suffix.lower() or ".mp4"
        dest = self.work_dir / f"{video.id}.screen{suffix}"
        try:
            log.info("audio check: downloading %s (%s)", video.name, _size(video.size))
            self.client.download(video.id, dest, video.size)
            text = self.transcriber_for(brand).transcribe(dest, self.work_dir)
            verdict = guard.judge_audio(self._guard_key(brand, video)[1], text)
        except (GoogleError, TranscribeError, GuardError) as exc:
            raise ContentError(f"{video.name}: audio check failed: {exc}") from exc
        finally:
            dest.unlink(missing_ok=True)
        self.state.kv_set(self._audio_key(brand, video), verdict.to_json())
        log.info("audio check %s [%s]: %s", video.name, verdict.risk, "; ".join(verdict.reasons))
        return verdict

    def _alert_hold(self, brand: str, stream: str, video: VideoItem, what: str, risk: str, shown: str,
                    reasons: tuple[str, ...]) -> None:
        self.notify(f"{self.app.brands[brand].name}/{stream}: held back by the {what} check ({risk}): «{shown}»\n"
                    f"{'; '.join(reasons)}\n{video.web_link or video.id}\n"
                    f"Rename the file to change this; it is checked again under its new name.",
                    f"guard-hold:{brand}:{video.id}")

    def skipped_title(self, brand: str, stream: str, video: VideoItem) -> bool:
        """True for files whose title is in skip_titles (e.g. a reel without a caption); alerts once per file."""
        src = self.app.brands[brand].text_source
        if not isinstance(src, FilenameTextSource) or not src.skip_titles:
            return False
        title = title_from_filename(video.name, src.strip_prefixes, src.replacements).strip()
        if title not in {t.strip() for t in src.skip_titles}:
            return False
        if video.id not in self._skipped:
            self._skipped.add(video.id)
            self.notify(f"{self.app.brands[brand].name}/{stream}: the file «{video.name}» has no caption, so it is "
                        f"not posted. Rename it to the caption you want and it will be posted in its turn.\n"
                        f"{video.web_link or video.id}", f"skip-title:{brand}:{video.id}")
        return True

    def backfill_boundary(self, brand: str, stream: str) -> int | None:
        """Highest Telegram msg_id of the initial backfill, or None while it is not complete."""
        cfg = self.app.brands[brand].streams[stream].backfill
        if cfg is None:
            return None
        if cfg.boundary_msg_id is not None:
            return cfg.boundary_msg_id
        key = f"backfill:{brand}:{stream}"
        stored = self.state.kv_get(key)
        if stored:
            return int(stored)
        ids = sorted(v.tg_msg_id for v in self.listing(brand, stream) if v.tg_msg_id is not None)
        if len(ids) < cfg.size:
            return None
        boundary = ids[cfg.size - 1]
        self.state.kv_set(key, str(boundary))
        self.notify(f"{self.app.brands[brand].name}/{stream}: the initial {cfg.size} reels are all in Drive "
                    f"(last msg_id {boundary}); later reels count as new in the daily summary.", key)
        return boundary

    def is_new(self, video: VideoItem, boundary: int | None) -> bool:
        return boundary is not None and video.tg_msg_id is not None and video.tg_msg_id > boundary

    def stream_stats(self, brand: str, stream: str, day) -> dict | None:
        """Daily-summary figures for a tg2drive stream (one with `backfill`), else None."""
        cfg = self.app.brands[brand].streams[stream].backfill
        if cfg is None:
            return None
        boundary = self.backfill_boundary(brand, stream)
        listing = self.listing(brand, stream)
        received = [v for v in listing if v.created and v.created.astimezone(self.tz).date() == day]
        queue = self.unused(brand, stream)
        return {
            "received": len(received),
            "received_new": sum(1 for v in received if self.is_new(v, boundary)),
            "posted": self.state.count_posts(brand, stream, day),
            "queue": len(queue),
            "queue_new": sum(1 for v in queue if self.is_new(v, boundary)),
            "backfill_size": cfg.size,
            "backfill_done": boundary is not None,
            "in_folder": sum(1 for v in listing if v.tg_msg_id is not None),
        }

    def next_video(self, brand: str, stream: str, slot: str | None = None) -> VideoItem | None:
        pending = self.unused(brand, stream)
        cfg = self.app.brands[brand].title_guard
        if not pending or cfg is None or not cfg.enabled:
            return dataclasses.replace(pending[0]) if pending else None
        audio = self.audio_on(brand, stream)
        fresh = 0
        for video in pending[: self.GUARD_LOOKAHEAD]:
            verdict = self.verdict(brand, video)          # a model failure raises: nothing is posted (fail closed)
            if verdict is not None and verdict.risk in cfg.block_levels:
                self._alert_hold(brand, stream, video, "title", verdict.risk, verdict.original, verdict.reasons)
                continue
            if audio and verdict is not None:
                if self.cached_audio(brand, video) is None:
                    if fresh >= self.AUDIO_FRESH_LIMIT:
                        raise ContentError(f"{self.AUDIO_FRESH_LIMIT} videos in a row were held back by the audio "
                                           f"check; the next run continues with the following ones")
                    fresh += 1
                heard = self.audio_verdict(brand, stream, video)
                if heard.risk in cfg.block_levels:
                    self._alert_hold(brand, stream, video, "audio", heard.risk, heard.excerpt, heard.reasons)
                    continue
            return dataclasses.replace(video)
        raise ContentError(f"the next {self.GUARD_LOOKAHEAD} videos were all held back by the title/audio check")

    def prescreen(self, brand: str, stream: str, limit: int, deadline: float | None = None) -> dict[str, int]:
        """Run the title and audio checks on the next `limit` videos of the stream, in posting order, so the
        publishing run only finds cached verdicts. Stops at `deadline` (time.monotonic); a model failure
        raises ContentError (the verdicts stored so far stay in state)."""
        stats = {"checked": 0, "held": 0, "already": 0}
        cfg = self.app.brands[brand].title_guard
        if cfg is None or not cfg.enabled or self.guard_for(brand) is None:
            return stats
        audio = self.audio_on(brand, stream)
        for video in self.unused(brand, stream)[:limit]:
            if deadline is not None and time.monotonic() >= deadline:
                break
            before = (self.state.kv_get(self._guard_key(brand, video)[0]) is not None,
                      not audio or self.cached_audio(brand, video) is not None)
            if all(before):
                stats["already"] += 1
                continue
            verdict = self.verdict(brand, video)
            held = None
            if verdict is not None and verdict.risk in cfg.block_levels:
                held = ("title", verdict.risk, verdict.original, verdict.reasons)
            elif audio:
                heard = self.audio_verdict(brand, stream, video)
                if heard.risk in cfg.block_levels:
                    held = ("audio", heard.risk, heard.excerpt, heard.reasons)
            stats["checked"] += 1
            if held:
                stats["held"] += 1
                self._alert_hold(brand, stream, video, *held)
        return stats

    def remaining(self, brand: str, stream: str) -> int:
        return len(self.unused(brand, stream))

    def video_by_id(self, brand: str, stream: str, source_id: str) -> VideoItem | None:
        for v in self.listing(brand, stream):
            if v.id == source_id:
                return dataclasses.replace(v)
        self._stream_of[source_id] = (brand, stream)
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
        style = self.frame_style_for(video)
        try:
            original = self.client.download(video.id, dest, video.size)
            if style is not None:
                before = probe(original)
                prepared = render_video(original, self.work_dir / f"{video.id}.framed.mp4", style, before,
                                        self.work_dir, self.encode.timeout_seconds)
                after = probe(prepared)
            else:
                prepared, before, after = prepare_for_upload(original, self.work_dir, self.encode_for(video))
        except (GoogleError, ProbeError, TranscodeError) as exc:
            dest.unlink(missing_ok=True)
            raise ContentError(f"{video.name}: {exc}") from exc
        log.info("media for %s: original %s | upload %s", video.name, before.describe(), after.describe())
        files = [original, prepared]
        where = self._stream_of.get(video.id)
        if where and self.app.brands[where[0]].streams[where[1]].cover == "random_frame":
            cover = pick_cover(prepared, after.duration_s, self.work_dir)
            if cover is not None:
                self._covers[video.id] = cover
                files.append(cover.image_path)
            else:
                log.warning("no cover frame for %s; platforms use their default cover", video.name)
        self._prepared[video.id] = (prepared, after, files)
        return prepared

    def cover(self, video: VideoItem) -> Cover | None:
        """The random cover frame chosen when the video was fetched (None when the stream has no cover rule)."""
        return self._covers.get(video.id)

    def frame_style_for(self, video: VideoItem) -> FrameStyle | None:
        where = self._stream_of.get(video.id)
        cfg = self.app.brands[where[0]].streams[where[1]].frame if where else None
        if cfg is None:
            return None
        return FrameStyle(logo=resolve_asset(cfg.logo), credit=cfg.credit, font=resolve_asset(cfg.font),
                          bg=cfg.bg, navy=cfg.navy, gold=cfg.gold, crf=cfg.crf, preset=self.encode.preset,
                          audio_bitrate="160k")

    def encode_for(self, video: VideoItem) -> EncodeSettings:
        where = self._stream_of.get(video.id)
        if where is None:
            return self.encode
        m = self.app.brands[where[0]].streams[where[1]].media
        return dataclasses.replace(self.encode, transcode=m.transcode, upscale=m.upscale)

    def release(self, video: VideoItem) -> None:
        _, _, files = self._prepared.pop(video.id, (None, None, []))
        self._covers.pop(video.id, None)
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
            verdict = self.verdict(brand, video)
            if verdict is not None:
                title = verdict.title
            return TextChoice(variables={"title": title})
        row = choose_next_row(self.sheet_rows(brand), self.state.caption_cursor(brand), src.start_row)
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
