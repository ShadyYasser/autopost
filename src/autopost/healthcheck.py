"""healthchecks.io pings: /start when a run begins, the plain URL on success, /fail on error.

A ping must never slow down or break a run: short timeout, every error swallowed, URL never logged.
"""

from __future__ import annotations

import logging

import requests

log = logging.getLogger(__name__)


class Healthcheck:
    def __init__(self, url: str | None, session: requests.Session | None = None, timeout: float = 10):
        self.url = (url or "").rstrip("/")
        self.session = session or requests.Session()
        self.timeout = timeout

    def _ping(self, suffix: str, body: str = "") -> bool:
        if not self.url:
            return False
        try:
            self.session.post(self.url + suffix, data=body.encode("utf-8")[:100_000], timeout=self.timeout)
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("healthcheck ping%s failed: %s", suffix or " (success)", type(exc).__name__)
            return False

    def start(self) -> bool:
        return self._ping("/start")

    def success(self, body: str = "") -> bool:
        return self._ping("", body)

    def fail(self, body: str = "") -> bool:
        return self._ping("/fail", body)
