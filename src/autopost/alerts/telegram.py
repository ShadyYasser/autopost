"""Telegram notifications (alerts, manual hand-offs, daily summary)."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import requests

if TYPE_CHECKING:
    from ..state import State

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"
MAX_TEXT = 4096
MAX_CAPTION = 1024
MAX_BOT_UPLOAD = 50 * 1024 * 1024   # Bot API upload limit


class Notifier:
    """Sends Telegram messages. In dry-run (or without credentials) messages are only logged."""

    def __init__(self, token: str | None, chat_id: str | None, dry_run: bool = False,
                 session: requests.Session | None = None):
        self.token = token
        self.chat_id = chat_id
        self.dry_run = dry_run or not (token and chat_id)
        self.session = session or requests.Session()
        self.sent: list[str] = []          # what was (or would have been) sent in this run

    @staticmethod
    def _markup(buttons: list[list[tuple[str, str]]] | None) -> str | None:
        """Inline keyboard: rows of (label, callback_data); callback_data is at most 64 bytes."""
        if not buttons:
            return None
        return json.dumps({"inline_keyboard": [[{"text": label, "callback_data": data} for label, data in row]
                                               for row in buttons]}, ensure_ascii=False)

    def send(self, text: str, buttons: list[list[tuple[str, str]]] | None = None) -> bool:
        if len(text) > MAX_TEXT:
            text = text[: MAX_TEXT - 1] + "…"
        self.sent.append(text)
        if self.dry_run:
            log.info("[telegram%s] %s", " dry-run" if self.token else " not configured", text.replace("\n", " ⏎ "))
            return True
        try:
            resp = self.session.post(
                API.format(token=self.token, method="sendMessage"),
                data={"chat_id": self.chat_id, "text": text, "disable_web_page_preview": "true",
                      **({"reply_markup": self._markup(buttons)} if buttons else {})},
                timeout=30,
            )
            if resp.status_code != 200:
                # Never log the URL: it contains the bot token.
                log.error("telegram sendMessage failed: HTTP %s %s", resp.status_code, resp.text[:300])
                return False
            return True
        except requests.RequestException as exc:
            log.error("telegram sendMessage failed: %s", type(exc).__name__)
            return False

    def _post(self, method: str, data: dict, files: dict | None = None, timeout: int = 30) -> dict | None:
        try:
            resp = self.session.post(API.format(token=self.token, method=method), data=data, files=files,
                                     timeout=timeout)
        except requests.RequestException as exc:
            log.error("telegram %s failed: %s", method, type(exc).__name__)   # never log the URL (token)
            return None
        if resp.status_code != 200:
            log.error("telegram %s failed: HTTP %s %s", method, resp.status_code, resp.text[:300])
            return None
        return resp.json()

    # -- bot control (buttons): used by `telegram-control`, a no-op in dry-run -------------------------------

    def get_updates(self, offset: int, timeout: int) -> list[dict] | None:
        """New updates (messages and button presses); None on an API error."""
        if self.dry_run:
            return []
        data = {"offset": offset, "timeout": timeout, "allowed_updates": json.dumps(["message", "callback_query"])}
        resp = self._post("getUpdates", data, timeout=timeout + 15)
        return None if resp is None else resp.get("result", [])

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        if not self.dry_run:
            self._post("answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:200]})

    def edit(self, message_id: int, text: str, buttons: list[list[tuple[str, str]]] | None = None) -> bool:
        """Replace a message's text (and buttons; none removes them)."""
        if self.dry_run:
            self.sent.append(text)
            return True
        data = {"chat_id": self.chat_id, "message_id": message_id, "text": text[:MAX_TEXT],
                "disable_web_page_preview": "true"}
        markup = self._markup(buttons)
        if markup:
            data["reply_markup"] = markup
        return self._post("editMessageText", data) is not None

    def get_me(self) -> str | None:
        """Bot username if the token works (for `check`)."""
        if not self.token:
            return None
        try:
            resp = self.session.get(API.format(token=self.token, method="getMe"), timeout=30)
            return resp.json().get("result", {}).get("username") if resp.status_code == 200 else None
        except requests.RequestException:
            return None

    def send_video(self, path: Path, caption: str, fallback_link: str | None = None) -> bool:
        """Send a video file with its caption (manual-post fallback). Over 50 MB → link + caption instead."""
        path = Path(path)
        size = path.stat().st_size if path.exists() else 0
        if self.dry_run:
            log.info("[telegram dry-run] video %s (%.1f MB) + caption", path.name, size / 1_048_576)
            self.sent.append(caption)
            return True
        if not size or size > MAX_BOT_UPLOAD:
            note = f"(video is {size / 1_048_576:.0f} MB, above Telegram's 50 MB bot limit)\n" if size else ""
            return self.send(f"{note}{fallback_link or ''}\n\n{caption}".strip())
        short = caption if len(caption) <= MAX_CAPTION else caption[: MAX_CAPTION - 1] + "…"
        with path.open("rb") as fh:
            ok = self._post("sendVideo", {"chat_id": self.chat_id, "caption": short, "supports_streaming": "true"},
                            files={"video": (path.name, fh, "video/mp4")}, timeout=600) is not None
        if ok and short != caption:
            ok = self.send(caption)   # full caption as a separate message
        return ok or self.send(f"{fallback_link or ''}\n\n{caption}".strip())

    def alert(self, text: str, *, key: str | None = None, state: "State | None" = None,
              now: datetime | None = None) -> bool:
        """Send an alert; with `key`, send it at most once (dedup stored in state)."""
        if key and state is not None and state.alert_sent(key):
            log.info("alert %s already sent", key)
            return False
        ok = self.send("⚠️ " + text)
        if ok and key and state is not None and now is not None and not self.dry_run:
            state.mark_alert(key, now)
        return ok
