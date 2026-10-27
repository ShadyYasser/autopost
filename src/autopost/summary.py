"""Daily summary sent on Telegram (once a day, after settings.daily_summary_time)."""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta

from .config import AppConfig
from .state import State

log = logging.getLogger(__name__)

ICON = {"sent": "✅", "manual": "✋", "failed": "❌", "gave_up": "❌", "skipped_deadline": "⏰",
        "pending": "⏳", "checking": "⏳"}


def build_daily_summary(app: AppConfig, state: State, content, day: date, now: datetime) -> str:
    tz = app.settings.tz
    totals = {"sent": 0, "manual": 0, "failed": 0}
    lines = [f"📊 ملخص يوم {day.isoformat()}"]
    for brand in app.enabled_brands():
        lines.append(f"\n▪️ {brand.name}")
        for s in state.slots(brand=brand.id, days=[day]):
            when = s.target.astimezone(tz).strftime("%H:%M")
            posts = state.posts_for_slot(brand.id, day, s.slot)
            if not posts:
                if s.status == "scheduled":
                    lines.append(f"  • {s.slot} {when}: لم يحن موعده بعد")
                else:
                    lines.append(f"  • {s.slot} {when}: ⏭️ تم التخطي ({s.note or s.status})")
                continue
            for p in posts:
                marks = []
                for d in state.deliveries(p["id"]):
                    marks.append(f"{d.platform}{ICON.get(d.status, '?')}")
                    if d.status == "sent":
                        totals["sent"] += 1
                    elif d.status == "manual":
                        totals["manual"] += 1
                    elif d.status in ("failed", "gave_up", "skipped_deadline"):
                        totals["failed"] += 1
                name = p["source_name"] if len(p["source_name"]) <= 40 else p["source_name"][:39] + "…"
                lines.append(f"  • {s.slot} {when} [{p['stream']}] {name}: {' '.join(marks)}")
        for stream_name, stream in brand.streams.items():
            try:
                left = content.remaining(brand.id, stream_name)
            except Exception as exc:  # noqa: BLE001
                lines.append(f"  المتبقي ({stream_name}): غير معروف ({exc})")
                continue
            days = left / max(1, len(stream.slots))
            warn = " ⚠️ أضف محتوى" if days <= app.settings.low_content_warning_days else ""
            lines.append(f"  المتبقي ({stream_name}): {left} فيديو ≈ {days:.0f} يوم{warn}")
    usage = []
    for name, acct in app.settings.buffer_accounts.items():
        used = state.buffer_requests_since(name, now - timedelta(days=1))
        usage.append(f"{name} {used}/{acct.limits.per_day}")
    lines.append(f"\nBuffer (آخر 24 ساعة): {' · '.join(usage)}")
    lines.append(f"الإجمالي: ✅ {totals['sent']} منشور · ✋ {totals['manual']} يدوي · ❌ {totals['failed']} فشل")
    return "\n".join(lines)


def summary_due(app: AppConfig, state: State, now: datetime) -> date | None:
    """The local date to summarise if today's summary is due and not sent yet."""
    local = now.astimezone(app.settings.tz)
    hh, mm = map(int, app.settings.daily_summary_time.split(":"))
    if local.time() < time(hh, mm):
        return None
    if state.kv_get("daily-summary") == local.date().isoformat():
        return None
    return local.date()


def send_daily_summary(app: AppConfig, state: State, content, notifier, now: datetime) -> bool:
    day = summary_due(app, state, now)
    if day is None:
        return False
    text = build_daily_summary(app, state, content, day, now)
    if notifier.send(text):
        state.kv_set("daily-summary", day.isoformat())
        log.info("daily summary sent for %s", day)
        return True
    return False
