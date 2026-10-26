"""Telegram notifications (alerts, manual hand-offs, daily summary)."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING

import requests

if TYPE_CHECKING:
    from ..state import State

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"
MAX_TEXT = 4096


class Notifier:
    """Sends Telegram messages. In dry-run (or without credentials) messages are only logged."""

    def __init__(self, token: str | None, chat_id: str | None, dry_run: bool = False,
                 session: requests.Session | None = None):
        self.token = token
        self.chat_id = chat_id
        self.dry_run = dry_run or not (token and chat_id)
        self.session = session or requests.Session()
        self.sent: list[str] = []          # what was (or would have been) sent in this run

    def send(self, text: str) -> bool:
        if len(text) > MAX_TEXT:
            text = text[: MAX_TEXT - 1] + "…"
        self.sent.append(text)
        if self.dry_run:
            log.info("[telegram%s] %s", " dry-run" if self.token else " not configured", text.replace("\n", " ⏎ "))
            return True
        try:
            resp = self.session.post(
                API.format(token=self.token, method="sendMessage"),
                data={"chat_id": self.chat_id, "text": text, "disable_web_page_preview": "true"},
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
