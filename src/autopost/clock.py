"""Real and simulated clocks (the simulated one lets dry runs and tests skip the waiting)."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)


class RealClock:
    simulated = False

    def __init__(self, tz: ZoneInfo):
        self.tz = tz

    def now(self) -> datetime:
        return datetime.now(self.tz).replace(microsecond=0)

    def sleep_until(self, when: datetime) -> None:
        seconds = (when - datetime.now(self.tz)).total_seconds()
        if seconds > 0:
            log.info("sleeping %.0fs until %s", seconds, when.astimezone(self.tz).strftime("%H:%M:%S"))
            time.sleep(seconds)

    def advance(self, seconds: float) -> None:
        """Real time passes on its own."""


class SimClock:
    simulated = True

    def __init__(self, start: datetime):
        if start.tzinfo is None:
            raise ValueError("SimClock needs an aware datetime")
        self._now = start.replace(microsecond=0)

    def now(self) -> datetime:
        return self._now

    def sleep_until(self, when: datetime) -> None:
        if when > self._now:
            self._now = when.replace(microsecond=0)

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=round(seconds))
