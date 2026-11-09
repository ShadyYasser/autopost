"""Manual control from Telegram: buttons for "publish now", "skip" and "pause automatic publishing".

The automatic schedule is untouched. This only adds a bot you can talk to:

  /menu    the next video of every stream, each with buttons: publish now (all platforms), publish to one
           platform, skip this video
  /status  paused or running, how many videos are waiting
  /pause   the scheduled runs publish nothing until /resume (housekeeping still runs)
  /resume

"Publish now" starts the same `test-post` run that is used for manual posts (workflow_dispatch of publish.yml):
the video is posted at once, recorded as delivered, and skipped for those platforms at its normal turn. "Skip"
and "pause" are stored in the state (`ctl:` keys) and honoured by the scheduled publisher.

Only messages from the configured Telegram chat are obeyed.
"""

from __future__ import annotations

import logging
import os
from typing import Callable

import requests

from .alerts.telegram import Notifier
from .config import AppConfig, FilenameTextSource
from .content.drive_source import ContentError, DriveContent
from .state import State
from .title_guard import Verdict

log = logging.getLogger(__name__)

PAUSED = "ctl:paused"
OFFSET = "ctl:offset"
SKIP = "ctl:skip:"

PLATFORM_LABELS = {"facebook": "فيسبوك", "instagram": "إنستجرام", "tiktok": "تيك توك", "youtube": "يوتيوب",
                   "pinterest": "بنترست"}
PLATFORM_CODES = {"f": "facebook", "i": "instagram", "t": "tiktok", "y": "youtube", "p": "pinterest"}
CODE_OF = {v: k for k, v in PLATFORM_CODES.items()}

# (brand, stream, platforms comma separated, video id) -> error text, or None when GitHub accepted the run
Dispatcher = Callable[[str, str, str, str], "str | None"]


def github_dispatcher() -> Dispatcher:
    """Start publish.yml (command test-post) through the GitHub API with the workflow's own token."""
    def dispatch(brand: str, stream: str, platforms: str, video_id: str) -> str | None:
        repo, token = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_TOKEN")
        if not (repo and token):
            return "GITHUB_REPOSITORY / GITHUB_TOKEN are not set"
        body = {"ref": os.environ.get("GITHUB_REF_NAME") or "main",
                "inputs": {"dry_run": "false", "command": "test-post", "brand": brand, "stream": stream,
                           "platform": platforms, "video": video_id}}
        try:
            resp = requests.post(f"https://api.github.com/repos/{repo}/actions/workflows/publish.yml/dispatches",
                                 json=body, timeout=30, headers={"Authorization": f"Bearer {token}",
                                                                 "Accept": "application/vnd.github+json"})
        except requests.RequestException as exc:
            return f"GitHub unreachable: {type(exc).__name__}"
        return None if resp.status_code == 204 else f"GitHub answered HTTP {resp.status_code}: {resp.text[:200]}"
    return dispatch


class Controller:
    def __init__(self, app: AppConfig, state: State, content: DriveContent, notifier: Notifier,
                 dispatcher: Dispatcher, on_change: Callable[[], None] | None = None):
        self.app, self.state, self.content, self.notifier = app, state, content, notifier
        self.dispatcher = dispatcher
        self.on_change = on_change or (lambda: None)       # called after a ctl: key changed (commit the state)

    # ------------------------------------------------------------ entry point

    def handle(self, update: dict) -> None:
        """One Telegram update. Never raises: a problem is reported in the chat."""
        try:
            if "callback_query" in update:
                self._callback(update["callback_query"])
            elif "message" in update:
                self._message(update["message"])
        except Exception as exc:  # noqa: BLE001 - the listener must survive any single update
            log.exception("control update failed")
            self.notifier.send(f"⚠️ تعذّر تنفيذ الأمر: {type(exc).__name__}: {str(exc)[:200]}")

    def _allowed(self, chat_id) -> bool:
        return bool(self.notifier.chat_id) and str(chat_id) == str(self.notifier.chat_id)

    # ------------------------------------------------------------ messages

    def _message(self, msg: dict) -> None:
        if not self._allowed(msg.get("chat", {}).get("id")):
            return
        command = (msg.get("text") or "").strip().split("@")[0].split()[:1]
        command = command[0].lower() if command else ""
        if command in ("/menu", "/start", "/queue"):
            self.menu()
        elif command == "/status":
            self.notifier.send(self._status_text(), self._pause_buttons())
        elif command == "/pause":
            self._set_paused(True)
            self.notifier.send(self._status_text(), self._pause_buttons())
        elif command == "/resume":
            self._set_paused(False)
            self.notifier.send(self._status_text(), self._pause_buttons())

    def _callback(self, cb: dict) -> None:
        msg = cb.get("message") or {}
        if not self._allowed(msg.get("chat", {}).get("id")):
            return
        message_id = msg.get("message_id")
        action, _, rest = (cb.get("data") or "").partition(":")
        if action in ("pause", "resume"):
            self._set_paused(action == "pause")
            self.notifier.answer_callback(cb["id"], "تم")
            self.notifier.edit(message_id, self._status_text(), self._pause_buttons())
            return
        video_id, _, extra = rest.partition(":")
        found = self.find(video_id)
        if action == "u":                                       # undo a skip: the video may be gone from the queue
            self.state.kv_set(SKIP + video_id, "")
            self.on_change()
            self.notifier.answer_callback(cb["id"], "اتلغى التخطي")
            self.notifier.edit(message_id, "↩️ اتلغى تخطي الفيديو، هيرجع للطابور.")
            return
        if found is None:
            self.notifier.answer_callback(cb["id"], "الفيديو لم يعد في الطابور")
            self.notifier.edit(message_id, "الفيديو ده لم يعد في الطابور (اتنشر أو اتغيّر). ابعت /menu.")
            return
        brand, stream, video = found
        if action == "p":
            platforms = ",".join(self.app.brands[brand].enabled_platforms(stream))
            self._publish(cb, message_id, brand, stream, video, platforms)
        elif action == "q":
            self._publish(cb, message_id, brand, stream, video, PLATFORM_CODES[extra])
        elif action == "c":
            self.notifier.answer_callback(cb["id"])
            rows = [[(PLATFORM_LABELS.get(p, p), f"q:{video_id}:{CODE_OF[p]}")]
                    for p in self.app.brands[brand].enabled_platforms(stream) if p in CODE_OF]
            rows.append([("رجوع", f"b:{video_id}")])
            self.notifier.edit(message_id, "انشر على أنهي منصة؟\n" + self._card_text(brand, stream, video), rows)
        elif action == "b":
            self.notifier.answer_callback(cb["id"])
            self.notifier.edit(message_id, self._card_text(brand, stream, video),
                               self._card_buttons(brand, stream, video))
        elif action == "s":
            self.state.kv_set(SKIP + video_id, "1")
            self.on_change()
            self.notifier.answer_callback(cb["id"], "اتخطّى")
            self.notifier.edit(message_id, "⏭ اتخطّى الفيديو ده ومش هيتنشر تلقائياً:\n" + self._title_of(brand, video),
                               [[("↩️ إلغاء التخطي", f"u:{video_id}")]])

    # ------------------------------------------------------------ actions

    def _publish(self, cb: dict, message_id, brand: str, stream: str, video, platforms: str) -> None:
        error = self.dispatcher(brand, stream, platforms, video.id)
        if error:
            self.notifier.answer_callback(cb["id"], "فشل")
            self.notifier.edit(message_id, f"⚠️ مقدرتش أبدأ النشر: {error}")
            return
        labels = "، ".join(PLATFORM_LABELS.get(p, p) for p in platforms.split(","))
        self.notifier.answer_callback(cb["id"], "اتبعت للنشر")
        self.notifier.edit(message_id, f"🚀 بدأ النشر على {labels}:\n{self._title_of(brand, video)}\n"
                                       f"هتوصلك رسالة بالنتيجة بعد دقائق (غالباً من 3 إلى 10).")

    def _set_paused(self, paused: bool) -> None:
        self.state.kv_set(PAUSED, "1" if paused else "")
        self.on_change()

    # ------------------------------------------------------------ views

    def menu(self) -> None:
        for brand in self.app.enabled_brands():
            for stream in brand.streams:
                try:
                    pending = self.content.unused(brand.id, stream)
                except ContentError as exc:
                    self.notifier.send(f"⚠️ {brand.name}/{stream}: {exc}")
                    continue
                if not pending:
                    self.notifier.send(f"{brand.name}/{stream}: مفيش فيديوهات في الطابور.")
                    continue
                video = pending[0]
                self.notifier.send(self._card_text(brand.id, stream, video, len(pending)),
                                   self._card_buttons(brand.id, stream, video))
        self.notifier.send(self._status_text(), self._pause_buttons())

    def find(self, video_id: str):
        for brand in self.app.enabled_brands():
            for stream in brand.streams:
                try:
                    for v in self.content.listing(brand.id, stream):
                        if v.id == video_id and v.id not in self.state.used_source_ids(brand.id, stream):
                            return brand.id, stream, v
                except ContentError:
                    continue
        return None

    def _title_of(self, brand: str, video) -> str:
        src = self.app.brands[brand].text_source
        if not isinstance(src, FilenameTextSource):
            return video.name
        key, original = self.content._guard_key(brand, video)
        raw = self.state.kv_get(key)
        if raw:
            try:
                verdict = Verdict.from_json(raw)
            except (ValueError, KeyError):
                return original
            if verdict.risk not in self.app.brands[brand].title_guard.block_levels:
                return verdict.title
            new = self.content.cached_retitle(brand, video)
            if new is not None and new.risk not in self.app.brands[brand].title_guard.block_levels:
                return new.title
        return original

    def _check_line(self, brand: str, stream: str, video) -> str:
        cfg = self.app.brands[brand].title_guard
        if cfg is None or not cfg.enabled or not isinstance(self.app.brands[brand].text_source, FilenameTextSource):
            return ""
        if self.content.checked(brand, stream, video):
            return "الفحص: ✅ تم"
        return "الفحص: ⏳ لسه ما اتفحصش (النشر الآن بيتخطّى فحص الصوت)"

    def _card_text(self, brand: str, stream: str, video, waiting: int | None = None) -> str:
        head = f"🎬 {self.app.brands[brand].name} / {stream}" + (f"  ({waiting} في الطابور)" if waiting else "")
        lines = [head, self._title_of(brand, video)]
        check = self._check_line(brand, stream, video)
        if check:
            lines.append(check)
        return "\n".join(lines)

    def _card_buttons(self, brand: str, stream: str, video) -> list[list[tuple[str, str]]]:
        return [[("▶️ انشر الآن", f"p:{video.id}"), ("🎯 منصة واحدة", f"c:{video.id}")],
                [("⏭ تخطّي الفيديو ده", f"s:{video.id}")]]

    def _paused(self) -> bool:
        return bool(self.state.kv_get(PAUSED))

    def _status_text(self) -> str:
        if self._paused():
            return "⏸ النشر التلقائي موقوف. الأزرار اليدوية شغالة."
        return "▶️ النشر التلقائي شغال."

    def _pause_buttons(self) -> list[list[tuple[str, str]]]:
        return [[("▶️ استئناف التلقائي", "resume")]] if self._paused() else [[("⏸ إيقاف التلقائي", "pause")]]
