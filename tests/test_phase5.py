from datetime import date, datetime, timedelta, timezone

import requests

from autopost.alerts.telegram import Notifier
from autopost.healthcheck import Healthcheck
from autopost.state import State
from autopost.summary import build_daily_summary, send_daily_summary, summary_due


class PingSession:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def post(self, url, data=None, timeout=None):
        self.calls.append((url, data, timeout))
        if self.fail:
            raise requests.ConnectionError("down")


def test_healthcheck_pings():
    s = PingSession()
    hc = Healthcheck("https://hc-ping.com/abc/", session=s)
    assert hc.start() and hc.success("published=3") and hc.fail("boom")
    assert [c[0] for c in s.calls] == ["https://hc-ping.com/abc/start", "https://hc-ping.com/abc",
                                       "https://hc-ping.com/abc/fail"]
    assert s.calls[1][1] == b"published=3" and s.calls[0][2] == 10


def test_healthcheck_never_breaks_the_run():
    hc = Healthcheck("https://hc-ping.com/abc", session=PingSession(fail=True))
    assert hc.start() is False and hc.fail("x") is False
    assert Healthcheck(None).start() is False          # not configured: silently off


class FakeContent:
    def remaining(self, brand, stream):
        return {"main": 452, "short": 7, "long": 99}[stream]

    def stream_stats(self, brand, stream, day):
        if stream != "short":
            return None
        return {"received": 5, "received_new": 4, "posted": 4, "queue": 7, "queue_new": 3,
                "backfill_size": 400, "backfill_done": True, "in_folder": 405}


def test_daily_summary_content(app, tmp_path):
    tz = app.settings.tz
    day = date(2026, 10, 27)
    now = datetime(2026, 10, 27, 23, 31, tzinfo=tz)
    state = State(tmp_path / "s.db")
    t = lambda h, m: datetime(2026, 10, 27, h, m, tzinfo=tz)  # noqa: E731
    state.insert_slot("takwin", day, "fajr", t(5, 21), t(5, 36), None, t(0, 7))
    state.insert_slot("takwin", day, "maghrib", t(17, 50), t(18, 8), None, t(0, 7))
    state.set_slot_status("takwin", day, "maghrib", "skipped", now, note="reached at 18:07, deadline 18:08")
    pid = state.create_post("takwin", "main", day, "fajr", "v1", "455.mp4", None, 2, {},
                            [("facebook", "direct"), ("tiktok", "buffer")], t(5, 21))
    fb, tk = state.deliveries(pid)
    state.update_delivery(fb.id, now, status="sent")
    state.update_delivery(tk.id, now, status="manual")
    state.record_buffer_request("tiktok_acct", now - timedelta(hours=3))
    text = build_daily_summary(app, state, FakeContent(), day, now)
    assert "455.mp4" in text and "facebook✅" in text and "tiktok✋" in text
    assert "تم التخطي" in text and "18:07" in text
    assert "452 فيديو" in text and "7 فيديو ≈ 2 يوم ⚠️" in text       # Kalima short: 7 left at 3/day
    assert "tiktok_acct 1/100" in text and "✅ 1 منشور" in text
    assert "وصل اليوم 5 (جديد 4) · نُشر اليوم 4 · في الطابور 7: 3 جديد + 4 من أول 400" in text


def test_summary_sent_once_after_its_time(app, tmp_path):
    state = State(tmp_path / "s.db")
    tz = app.settings.tz
    before = datetime(2026, 10, 27, 23, 0, tzinfo=tz)
    after = datetime(2026, 10, 27, 23, 35, tzinfo=tz)
    assert summary_due(app, state, before) is None
    notifier = Notifier(None, None, dry_run=True)
    assert send_daily_summary(app, state, FakeContent(), notifier, after)
    assert not send_daily_summary(app, state, FakeContent(), notifier, after + timedelta(minutes=10))
    assert len(notifier.sent) == 1
