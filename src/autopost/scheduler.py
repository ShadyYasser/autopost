"""Daily slot targets.

Each brand's slots are computed once per local date and stored in state. A slot
belongs to the date it is computed for, even when its target falls after midnight
(e.g. a night window running from Maghrib + 3h to the next day's Fajr - 3h).
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Callable
from zoneinfo import ZoneInfo

from .config import (
    AppConfig,
    Anchor,
    BrandConfig,
    FixedSlot,
    PickWindowSlot,
    PrayerSlot,
    RandomWindowSlot,
    Window,
)
from .prayer import PrayerCalculator

log = logging.getLogger(__name__)

PrayerLookup = Callable[[date, str], datetime]


@dataclass(frozen=True)
class SlotPlan:
    brand: str
    slot_date: date
    slot: str
    target: datetime
    deadline: datetime | None
    window: str | None = None


class ScheduleError(Exception):
    pass


def _no_prayers(day: date, prayer: str) -> datetime:
    raise ScheduleError("this slot needs prayer times but none are configured")


def resolve_anchor(anchor: Anchor, day: date, tz: ZoneInfo, prayer: PrayerLookup) -> datetime:
    on = day + timedelta(days=anchor.day_offset)
    if anchor.time is not None:
        hh, mm = map(int, anchor.time.split(":"))
        base = datetime.combine(on, time(hh, mm), tzinfo=tz)
    else:
        base = prayer(on, anchor.prayer)
    return base + timedelta(minutes=anchor.offset_minutes)


def _jitter(rng: random.Random, minutes: int) -> timedelta:
    return timedelta(seconds=round(rng.uniform(-minutes * 60, minutes * 60)))


def _in_window(window: Window, day: date, tz: ZoneInfo, prayer: PrayerLookup, rng: random.Random) -> datetime:
    start = resolve_anchor(window.start, day, tz, prayer)
    end = resolve_anchor(window.end, day, tz, prayer)
    if end <= start:
        raise ScheduleError(f"window {window.label or ''} is empty on {day}: {start:%H:%M} → {end:%H:%M}")
    return start + timedelta(seconds=round(rng.uniform(0, (end - start).total_seconds())))


def compute_slot(slot, day: date, tz: ZoneInfo, prayer: PrayerLookup, rng: random.Random):
    """Return (target, deadline, window_label) for one slot on one day."""
    window_label = None
    deadline = None
    if isinstance(slot, FixedSlot):
        target = resolve_anchor(Anchor(time=slot.at), day, tz, prayer) + _jitter(rng, slot.jitter_minutes)
    elif isinstance(slot, PrayerSlot):
        p = prayer(day, slot.prayer)
        target = p + timedelta(minutes=slot.offset_minutes) + _jitter(rng, slot.jitter_minutes)
        if slot.hard_deadline:
            deadline = p
    elif isinstance(slot, RandomWindowSlot):
        target = _in_window(slot.window, day, tz, prayer, rng)
        window_label = slot.window.label
    elif isinstance(slot, PickWindowSlot):
        usable = [
            w for w in slot.windows
            if resolve_anchor(w.end, day, tz, prayer) > resolve_anchor(w.start, day, tz, prayer)
        ]
        if not usable:
            raise ScheduleError(f"no usable window on {day}")
        chosen = rng.choices(usable, weights=[w.weight for w in usable])[0]
        target = _in_window(chosen, day, tz, prayer, rng)
        window_label = chosen.label
    else:  # pragma: no cover - guarded by the config union
        raise ScheduleError(f"unknown slot type {type(slot).__name__}")

    if slot.deadline is not None:
        deadline = resolve_anchor(slot.deadline, day, tz, prayer)
    if deadline is not None and target >= deadline:
        raise ScheduleError(f"target {target} is not before its deadline {deadline}")
    return target, deadline, window_label


def _spaced(plans: list[SlotPlan], min_gap: timedelta) -> bool:
    targets = sorted(p.target for p in plans)
    return all(b - a >= min_gap for a, b in zip(targets, targets[1:]))


def compute_day(
    brand: BrandConfig,
    day: date,
    tz: ZoneInfo,
    prayer: PrayerLookup | None,
    rng: random.Random,
    min_spacing_minutes: int = 0,
    attempts: int = 200,
) -> list[SlotPlan]:
    prayer = prayer or _no_prayers
    min_gap = timedelta(minutes=min_spacing_minutes)
    plans: list[SlotPlan] = []
    for _ in range(attempts):
        plans = []
        for name, slot in brand.slots.items():
            target, deadline, label = compute_slot(slot, day, tz, prayer, rng)
            plans.append(SlotPlan(brand.id, day, name, target, deadline, label))
        if _spaced(plans, min_gap):
            break
    else:
        log.warning("%s %s: could not keep slots %s min apart; using last draw", brand.id, day, min_spacing_minutes)
    return sorted(plans, key=lambda p: p.target)


def prayer_lookup_for(app: AppConfig, brand: BrandConfig) -> PrayerLookup | None:
    cfg = app.prayer_config(brand)
    if cfg is None:
        return None
    return PrayerCalculator(cfg, app.settings.tz).time


def plan_brand_day(app: AppConfig, brand: BrandConfig, day: date, rng: random.Random) -> list[SlotPlan]:
    spacing = brand.min_slot_spacing_minutes
    if spacing is None:
        spacing = app.settings.scheduler.min_slot_spacing_minutes
    return compute_day(brand, day, app.settings.tz, prayer_lookup_for(app, brand), rng, spacing)
