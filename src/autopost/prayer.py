"""Offline prayer-time calculation (adhanpy)."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

from adhanpy.PrayerTimes import PrayerTimes
from adhanpy.calculation.CalculationMethod import CalculationMethod

from .config import PrayerTimesConfig

PRAYERS = ("fajr", "sunrise", "dhuhr", "asr", "maghrib", "isha")


class PrayerCalculator:
    def __init__(self, cfg: PrayerTimesConfig, tz: ZoneInfo):
        self.cfg = cfg
        self.tz = tz

    def times(self, day: date) -> dict[str, datetime]:
        return _times(self.cfg.method, self.cfg.latitude, self.cfg.longitude,
                      tuple(sorted(self.cfg.adjustments_minutes.items())), str(self.tz), day)

    def time(self, day: date, prayer: str) -> datetime:
        return self.times(day)[prayer]


@lru_cache(maxsize=256)
def _times(method: str, lat: float, lon: float, adjustments: tuple, tz_name: str, day: date) -> dict[str, datetime]:
    tz = ZoneInfo(tz_name)
    pt = PrayerTimes(
        (lat, lon),
        datetime(day.year, day.month, day.day),
        calculation_method=CalculationMethod[method],
        time_zone=tz,
    )
    adj = dict(adjustments)
    return {p: getattr(pt, p).astimezone(tz) + timedelta(minutes=adj.get(p, 0)) for p in PRAYERS}
