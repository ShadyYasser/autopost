import random
from datetime import date, timedelta

from autopost.prayer import PrayerCalculator
from autopost.scheduler import plan_brand_day


def test_damietta_prayer_times(app):
    times = PrayerCalculator(app.settings.prayer_times, app.settings.tz).times(date(2026, 10, 26))
    assert times["fajr"].strftime("%H:%M") == "05:35"     # summer time (UTC+3) still active
    assert times["maghrib"].strftime("%H:%M") == "18:09"
    after_dst = PrayerCalculator(app.settings.prayer_times, app.settings.tz).times(date(2026, 10, 30))
    assert after_dst["fajr"].strftime("%H:%M") == "04:38"


def test_takwin_prayer_slots_always_before_prayer(app):
    brand = app.brands["takwin"]
    calc = PrayerCalculator(app.settings.prayer_times, app.settings.tz)
    rng = random.Random(7)
    windows = set()
    for i in range(400):
        day = date(2026, 1, 1) + timedelta(days=i)
        plans = {p.slot: p for p in plan_brand_day(app, brand, day, rng)}
        times = calc.times(day)
        for slot, prayer in (("fajr", "fajr"), ("maghrib", "maghrib")):
            p = plans[slot]
            assert p.deadline == times[prayer]
            lead = (times[prayer] - p.target).total_seconds() / 60
            assert 10 <= lead <= 20, (day, slot, lead)
        third = plans["third"]
        windows.add(third.window)
        local = third.target.astimezone(app.settings.tz)
        if third.window == "day":
            assert local.date() == day and 12 <= local.hour < 16
        else:
            assert local >= times["maghrib"] + timedelta(hours=3)
            assert local <= calc.times(day + timedelta(days=1))["fajr"] - timedelta(hours=3)
        assert third.deadline is None
    assert windows == {"day", "night"}


def test_pick_window_is_roughly_fifty_fifty(app):
    brand = app.brands["takwin"]
    rng = random.Random(3)
    picks = [next(p.window for p in plan_brand_day(app, brand, date(2026, 3, 1), rng) if p.slot == "third")
             for _ in range(600)]
    assert 0.4 < picks.count("day") / len(picks) < 0.6


def test_kalima_fixed_slots_jitter_and_spacing(app):
    brand = app.brands["kalima_sawaa"]
    rng = random.Random(11)
    base = {"morning": (10, 0), "afternoon": (14, 0), "evening": (18, 30), "night": (22, 0)}
    for i in range(200):
        day = date(2026, 5, 1) + timedelta(days=i)
        plans = plan_brand_day(app, brand, day, rng)
        assert [p.slot for p in plans] == ["morning", "afternoon", "evening", "night"]
        for p in plans:
            h, m = base[p.slot]
            center = p.target.astimezone(app.settings.tz).replace(hour=h, minute=m, second=0)
            assert abs((p.target - center).total_seconds()) <= 45 * 60
            assert p.deadline is None
        gaps = [(b.target - a.target).total_seconds() for a, b in zip(plans, plans[1:])]
        assert min(gaps) >= 120 * 60
