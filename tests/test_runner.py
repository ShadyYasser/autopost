import random
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from autopost.alerts.telegram import Notifier
from autopost.clock import SimClock
from autopost.content.base import TextChoice, VideoItem
from autopost.platforms.base import Outcome
from autopost.runner import Runner
from autopost.secret_store import Secrets
from autopost.state import State


class FakeContent:
    def __init__(self, count=50):
        self.videos = {}
        self.count = count
        self.consumed = []
        self.released = []

    def _pool(self, brand, stream):
        return self.videos.setdefault((brand, stream), [
            VideoItem(id=f"{brand}-{stream}-{i}", name=f"{stream}-{i}.mp4", created=None) for i in range(self.count)])

    def next_video(self, brand, stream, slot=None):
        pool = self._pool(brand, stream)
        if not pool:
            return None
        v = pool.pop(0)
        self.consumed.append(v.id)
        return v

    def remaining(self, brand, stream):
        return len(self._pool(brand, stream))

    def video_by_id(self, brand, stream, source_id):
        return VideoItem(id=source_id, name=source_id, created=None)

    def pick_text(self, brand, stream, video):
        return TextChoice({"title": video.name, "caption": "caption " + video.name})

    def commit_text(self, brand, choice, when):
        pass

    def fetch(self, video):
        return Path("/dev/null")

    def release(self, video):
        self.released.append(video.id)


class FakePublisher:
    """Records publish times; `fail` platforms fail, `seconds` simulates upload time."""

    def __init__(self, clock, fail=(), seconds=10):
        self.clock = clock
        self.fail = set(fail)
        self.seconds = seconds
        self.calls = []

    def publish(self, ctx):
        self.calls.append((ctx.brand.id, ctx.stream, ctx.platform, ctx.cfg.provider, self.clock.now(), ctx.texts))
        self.clock.advance(self.seconds)
        if ctx.platform in self.fail:
            return Outcome(status="failed", error="boom")
        return Outcome(status="sent", external_id="x")

    def check(self, ctx, d):
        return Outcome(status="sent")

    def cancel(self, ctx, d):
        pass


TZ_DAY = date(2026, 10, 27)


@pytest.fixture
def app(app):
    """Shipped config with every platform switched on (Pinterest is off until its account is ready)."""
    for brand in app.brands.values():
        for cfg in brand.platforms.values():
            cfg.enabled = True
    return app


def make(app, tmp_path, now, content=None, pub=None, brand=None, seed=1, state=None):
    clock = SimClock(now)
    state = state or State(tmp_path / "state.db")
    content = content or FakeContent()
    pub = pub or FakePublisher(clock)
    pub.clock = clock
    notifier = Notifier(None, None, dry_run=True)
    runner = Runner(app, state, clock, notifier, content, lambda p, prov: pub, Secrets({}),
                    rng=random.Random(seed), brand_filter=brand)
    return runner, state, clock, content, pub, notifier


def at(app, hh, mm, day=TZ_DAY):
    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=app.settings.tz)


def slot(state, brand, name, day=TZ_DAY):
    return next(s for s in state.slots(brand=brand, days=[day]) if s.slot == name)


def prepare_slots(app, tmp_path, brand):
    """Create the day's slots early in the morning (as the first run of the day would)."""
    runner, state, *_ = make(app, tmp_path, at(app, 0, 7), brand=brand)
    runner.ensure_slots(at(app, 0, 7))
    return state


def test_prayer_slot_publishes_everything_before_deadline(app, tmp_path):
    state = prepare_slots(app, tmp_path, "takwin")
    s = slot(state, "takwin", "maghrib")
    runner, state, clock, content, pub, notifier = make(app, tmp_path, s.target - timedelta(minutes=10),
                                                         brand="takwin", state=state)
    runner.run()
    calls = [c for c in pub.calls if c[0] == "takwin"]
    assert sorted(c[2] for c in calls) == ["facebook", "instagram", "pinterest", "tiktok", "youtube"]
    assert calls[0][4] == s.target                       # lookahead: slept exactly until the target
    margin = timedelta(seconds=app.settings.scheduler.deadline_margin_seconds)
    assert all(c[4] < s.deadline - margin for c in calls)
    gaps = [(b[4] - a[4]).total_seconds() - pub.seconds for a, b in zip(calls, calls[1:])]
    assert all(g <= 120 for g in gaps)                   # short gaps for prayer slots
    assert slot(state, "takwin", "maghrib").status == "done"


def test_prayer_slot_reached_after_deadline_is_skipped_and_keeps_video(app, tmp_path):
    state = prepare_slots(app, tmp_path, "takwin")
    s = slot(state, "takwin", "fajr")
    runner, state, clock, content, pub, notifier = make(app, tmp_path, s.deadline + timedelta(minutes=1),
                                                         brand="takwin", state=state)
    runner.run()
    assert slot(state, "takwin", "fajr").status == "skipped"
    assert content.consumed == [] and pub.calls == []
    assert any("Slot skipped" in m and "fajr" in m for m in notifier.sent)


def test_slow_platforms_are_skipped_at_deadline_and_reported(app, tmp_path):
    state = prepare_slots(app, tmp_path, "takwin")
    s = slot(state, "takwin", "maghrib")
    pub = FakePublisher(None, seconds=240)               # each upload takes 4 minutes
    runner, state, clock, content, pub, notifier = make(app, tmp_path, s.target, brand="takwin",
                                                         state=state, pub=pub)
    runner.run()
    margin = timedelta(seconds=app.settings.scheduler.deadline_margin_seconds)
    assert all(c[4] < s.deadline - margin for c in pub.calls)
    post = state.posts_for_slot("takwin", TZ_DAY, "maghrib")[0]
    statuses = {d.platform: d.status for d in state.deliveries(post["id"])}
    skipped = [p for p, st in statuses.items() if st == "skipped_deadline"]
    assert skipped and len(skipped) + len(pub.calls) == 5
    assert any("not published before the deadline" in m for m in notifier.sent)
    assert slot(state, "takwin", "maghrib").status == "partial"


def test_kalima_night_posts_short_and_long(app, tmp_path):
    state = prepare_slots(app, tmp_path, "kalima_sawaa")
    s = slot(state, "kalima_sawaa", "night")
    runner, state, clock, content, pub, notifier = make(app, tmp_path, s.target, brand="kalima_sawaa", state=state)
    runner.run()
    by_stream = {}
    for brand, stream, platform, provider, *_ in pub.calls:
        by_stream.setdefault(stream, {})[platform] = provider
    assert set(by_stream["short"]) == {"facebook", "instagram", "tiktok", "youtube", "pinterest"}
    assert by_stream["long"] == {"youtube": "direct", "facebook": "direct"}
    assert by_stream["short"]["youtube"] == "buffer"
    texts = next(c[5] for c in pub.calls if c[1] == "long" and c[2] == "youtube")
    assert texts["title"] == texts["description"]


def test_failed_platform_is_retried_on_later_runs_then_given_up(app, tmp_path):
    state = prepare_slots(app, tmp_path, "kalima_sawaa")
    s = slot(state, "kalima_sawaa", "morning")
    now = s.target
    for run in range(4):
        pub = FakePublisher(None, fail={"instagram"})
        runner, state, clock, content, pub, notifier = make(app, tmp_path, now, brand="kalima_sawaa",
                                                             state=state, pub=pub)
        runner.run()
        insta = [c for c in pub.calls if c[2] == "instagram"]
        assert len(insta) == (1 if run < 3 else 0)
        now = clock.now() + timedelta(minutes=30)
    post = state.posts_for_slot("kalima_sawaa", TZ_DAY, "morning")[0]
    d = {x.platform: x for x in state.deliveries(post["id"])}
    assert d["instagram"].status == "gave_up" and d["instagram"].attempts == 3
    assert d["facebook"].status == "sent"
    assert post["status"] == "partial"
    assert slot(state, "kalima_sawaa", "morning").status == "partial"


def test_out_of_content_alerts_and_skips(app, tmp_path):
    state = prepare_slots(app, tmp_path, "kalima_sawaa")
    s = slot(state, "kalima_sawaa", "morning")
    runner, state, clock, content, pub, notifier = make(app, tmp_path, s.target, brand="kalima_sawaa",
                                                         state=state, content=FakeContent(count=0))
    runner.run()
    assert pub.calls == []
    assert slot(state, "kalima_sawaa", "morning").status == "skipped"
    assert any("no unposted videos" in m for m in notifier.sent)


def test_low_content_warning(app, tmp_path):
    state = prepare_slots(app, tmp_path, "kalima_sawaa")
    s = slot(state, "kalima_sawaa", "morning")
    runner, state, clock, content, pub, notifier = make(app, tmp_path, s.target, brand="kalima_sawaa",
                                                         state=state, content=FakeContent(count=8))
    runner.run()
    # short stream: 7 left after this post, 3 per day → 2.3 days ≤ 3
    assert any("only 7 unposted" in m for m in notifier.sent)


def test_late_regular_slot_is_skipped(app, tmp_path):
    state = prepare_slots(app, tmp_path, "kalima_sawaa")
    s = slot(state, "kalima_sawaa", "morning")
    late = s.target + timedelta(minutes=app.settings.scheduler.max_lateness_minutes + 1)
    runner, state, clock, content, pub, notifier = make(app, tmp_path, late, brand="kalima_sawaa", state=state)
    runner.run()
    assert slot(state, "kalima_sawaa", "morning").status == "skipped"
    assert not [c for c in pub.calls if c[4] < late]


def test_slots_computed_once_per_day(app, tmp_path):
    state = prepare_slots(app, tmp_path, "takwin")
    before = {(s.slot, s.target) for s in state.slots(brand="takwin", days=[TZ_DAY])}
    runner, state, *_ = make(app, tmp_path, at(app, 9, 37), brand="takwin", state=state, seed=999)
    runner.ensure_slots(at(app, 9, 37))
    after = {(s.slot, s.target) for s in state.slots(brand="takwin", days=[TZ_DAY])}
    assert before == after


def test_slots_created_after_their_time_are_skipped_quietly(app, tmp_path):
    runner, state, clock, content, pub, notifier = make(app, tmp_path, at(app, 23, 50), brand="takwin")
    runner.ensure_slots(at(app, 23, 50))
    yesterday = state.slots(brand="takwin", days=[TZ_DAY - timedelta(days=1)])
    assert yesterday and all(s.status == "skipped" for s in yesterday if s.window != "night")
    assert notifier.sent == []


class BrokenContent(FakeContent):
    def pick_text(self, brand, stream, video):
        raise RuntimeError("Sheets API unavailable")


class SheetWriteFails(FakeContent):
    def commit_text(self, brand, choice, when):
        raise RuntimeError("could not write the posted date to sheet row 7")


def test_content_error_keeps_slot_for_next_run(app, tmp_path):
    state = prepare_slots(app, tmp_path, "kalima_sawaa")
    s = slot(state, "kalima_sawaa", "morning")
    runner, state, clock, content, pub, notifier = make(app, tmp_path, s.target, brand="kalima_sawaa",
                                                         state=state, content=BrokenContent())
    runner.run()
    assert pub.calls == [] and state.recent_posts() == []
    assert slot(state, "kalima_sawaa", "morning").status == "scheduled"
    assert any("could not prepare content" in m and "retried" in m for m in notifier.sent)
    # next run, content is back
    runner, state, clock, content, pub, notifier = make(app, tmp_path, s.target + timedelta(minutes=30),
                                                         brand="kalima_sawaa", state=state)
    runner.run()
    assert slot(state, "kalima_sawaa", "morning").status == "done"


def test_sheet_write_failure_alerts_but_publishes(app, tmp_path):
    state = prepare_slots(app, tmp_path, "takwin")
    s = slot(state, "takwin", "third")
    runner, state, clock, content, pub, notifier = make(app, tmp_path, s.target, brand="takwin",
                                                         state=state, content=SheetWriteFails())
    runner.run()
    assert len([c for c in pub.calls if c[0] == "takwin"]) >= 5
    assert any("sheet row 7" in m for m in notifier.sent)


def test_busy_run_picks_up_a_prayer_slot_that_becomes_due(app, tmp_path):
    """A long Kalima post must not make Takwin's Maghrib wait for the next run."""
    runner, state, *_ = make(app, tmp_path, at(app, 0, 7))
    runner.ensure_slots(at(app, 0, 7))
    maghrib = slot(state, "takwin", "maghrib")
    # pretend a Kalima slot is due 30 min before Maghrib (outside Maghrib's lookahead at run start)
    state.db.execute("UPDATE daily_slots SET target_utc=?, status='scheduled' WHERE brand='kalima_sawaa' AND slot_date=? "
                     "AND slot='afternoon'", ((maghrib.target - timedelta(minutes=30)).astimezone(timezone.utc)
                                              .isoformat(timespec="seconds"), TZ_DAY.isoformat()))
    start = maghrib.target - timedelta(minutes=30)
    pub = FakePublisher(None, seconds=60)
    runner, state, clock, content, pub, notifier = make(app, tmp_path, start, state=state, pub=pub)
    runner.run()
    takwin = [c for c in pub.calls if c[0] == "takwin" and c[1] == "main"]
    assert len(takwin) == 5 and takwin[0][4] >= maghrib.target
    assert all(c[4] < maghrib.deadline for c in takwin)


def test_early_test_deliveries_are_skipped_and_texts_reused(app, tmp_path):
    state = prepare_slots(app, tmp_path, "kalima_sawaa")
    s = slot(state, "kalima_sawaa", "morning")
    content = FakeContent()
    first = content._pool("kalima_sawaa", "short")[0]
    state.record_early_delivery("kalima_sawaa", "short", first.id, "facebook", "direct", "sent", "FB1",
                                "https://fb/1", {"title": "from the test"}, s.target - timedelta(days=2))
    state.record_early_delivery("kalima_sawaa", "short", first.id, "instagram", "direct", "sent", "IG1",
                                "https://ig/1", {"title": "from the test"}, s.target - timedelta(days=2))
    runner, state, clock, content, pub, notifier = make(app, tmp_path, s.target, brand="kalima_sawaa",
                                                         state=state, content=content)
    runner.run()
    platforms = sorted(c[2] for c in pub.calls)
    assert platforms == ["pinterest", "tiktok", "youtube"]
    assert all(c[5].get("title", c[5].get("text")) == "from the test" for c in pub.calls)
    post = state.posts_for_slot("kalima_sawaa", TZ_DAY, "morning")[0]
    d = {x.platform: x for x in state.deliveries(post["id"])}
    assert d["facebook"].status == "sent" and d["facebook"].url == "https://fb/1"
    assert post["status"] == "done"


def test_ffmpeg_installed_only_when_a_post_is_due(app, tmp_path):
    state = prepare_slots(app, tmp_path, "kalima_sawaa")
    s = slot(state, "kalima_sawaa", "morning")
    calls = []
    # nothing due: no install
    runner, state, *_ = make(app, tmp_path, s.target - timedelta(hours=3), brand="kalima_sawaa", state=state)
    runner._ensure_tools = lambda: calls.append("install")
    runner.run()
    assert calls == []
    # slot within look-ahead: install before anything else, exactly once
    runner, state, clock, content, pub, notifier = make(app, tmp_path, s.target - timedelta(minutes=10),
                                                         brand="kalima_sawaa", state=state)
    runner._ensure_tools = lambda: calls.append(("install", clock.now()))
    runner.run()
    assert len(calls) == 1 and calls[0][1] == s.target - timedelta(minutes=10)
    assert pub.calls and pub.calls[0][4] >= calls[0][1]
