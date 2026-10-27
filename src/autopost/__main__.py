"""Command line entry point: python -m autopost <command>."""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import shutil
import subprocess
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

from .alerts.telegram import Notifier
from .clock import RealClock, SimClock
from .config import AppConfig, ConfigError, load_config, placeholder_values
from .content.base import PlaceholderContent
from .content.drive_source import ContentError, DriveContent, choose_next_row
from .content.google_api import GoogleClient, GoogleError, session_from_service_account
from .content.text import render
from .platforms.base import DryRunPublisher
from .prayer import PrayerCalculator
from .runner import Runner
from .scheduler import ScheduleError, plan_brand_day
from .secret_store import MissingSecret, Secrets
from .state import State

log = logging.getLogger("autopost")
ROOT = Path(__file__).resolve().parents[2]


class ClockFormatter(logging.Formatter):
    """Log timestamps in the brand timezone, taken from the run's clock (so dry runs show simulated time)."""

    def __init__(self, tz, now=None):
        super().__init__("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
        self.tz = tz
        self.now = now

    def formatTime(self, record, datefmt=None):  # noqa: N802 - logging API
        when = self.now() if self.now else datetime.fromtimestamp(record.created, self.tz)
        return when.astimezone(self.tz).strftime("%m-%d %H:%M:%S")


def use_clock_for_logs(tz, now=None) -> None:
    for handler in logging.getLogger().handlers:
        handler.setFormatter(ClockFormatter(tz, now))


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO)
    # Library loggers can print request URLs, and Telegram URLs contain the bot token.
    for noisy in ("urllib3", "requests", "googleapiclient", "google"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def make_notifier(app: AppConfig, secrets: Secrets, dry_run: bool) -> Notifier:
    tg = app.settings.telegram
    token = secrets.get(tg.bot_token_secret) if secrets.has(tg.bot_token_secret) else None
    chat = secrets.get(tg.chat_id_secret) if secrets.has(tg.chat_id_secret) else None
    if not (token and chat):
        log.warning("Telegram is not configured (%s / %s); alerts only go to the log",
                    tg.bot_token_secret, tg.chat_id_secret)
    return Notifier(token, chat, dry_run=dry_run)


def work_dir() -> Path:
    return Path(os.environ.get("RUNNER_TEMP") or ROOT / "work") / "autopost-media"


def open_state(path: str | Path, throwaway: bool) -> tuple[State, str | None]:
    """Open state; with throwaway=True work on a temporary copy that is deleted afterwards."""
    if not throwaway:
        return State(path), None
    tmpdir = tempfile.mkdtemp(prefix="autopost-dry-")
    copy = Path(tmpdir) / "state.db"
    if Path(path).exists():
        shutil.copy(path, copy)
    return State(copy), tmpdir


def make_content(app: AppConfig, state: State, secrets: Secrets, dry_run: bool, placeholder: bool = False,
                 notify=None):
    if placeholder:
        log.warning("using placeholder content (fake videos and texts)")
        return PlaceholderContent()
    name = app.settings.google.service_account_secret
    if not secrets.has(name):
        if dry_run:
            log.warning("%s is not set: dry run uses placeholder content", name)
            return PlaceholderContent()
        raise MissingSecret(name)
    session, email = session_from_service_account(secrets.get(name))
    log.info("Google: service account %s", email)
    return DriveContent(app, state, GoogleClient(session), work_dir(), dry_run, notify=notify)


def parse_now(value: str | None, app: AppConfig) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    return dt.replace(tzinfo=app.settings.tz) if dt.tzinfo is None else dt


# ---------------------------------------------------------------- commands


def cmd_validate(args, app: AppConfig) -> int:
    print(f"config OK: {len(app.brands)} brand(s): " + ", ".join(
        f"{b.id}{'' if b.enabled else ' (disabled)'}" for b in app.brands.values()))
    print(f"cron_enabled={app.settings.cron_enabled}  youtube_direct.enabled={app.settings.youtube_direct.enabled}"
          f"  pinterest={app.settings.pinterest.environment}")
    problems = 0
    for path in placeholder_values(app):
        print(f"  ! placeholder still set: {path}")
        problems += 1
    secrets = Secrets()
    for name, where in sorted(app.required_secrets().items()):
        ok = secrets.has(name)
        if args.check_secrets or ok:
            print(f"  {'✓' if ok else '✗'} secret {name}  (used by {', '.join(sorted(set(where)))})")
        if args.check_secrets and not ok:
            problems += 1
    if args.ping_telegram:
        notifier = make_notifier(app, secrets, dry_run=False)
        if notifier.dry_run or not notifier.send("✅ autopost: Telegram connection works."):
            print("  ✗ Telegram test message failed")
            problems += 1
        else:
            print("  ✓ Telegram test message sent")
    return 1 if problems and args.strict else 0


def cmd_plan(args, app: AppConfig) -> int:
    tz = app.settings.tz
    start = date.fromisoformat(args.date) if args.date else datetime.now(tz).date()
    rng = random.Random(args.seed) if args.seed is not None else random.SystemRandom()
    for i in range(args.days):
        day = start + timedelta(days=i)
        for brand in app.enabled_brands():
            if args.brand and brand.id != args.brand:
                continue
            pcfg = app.prayer_config(brand)
            extra = ""
            if pcfg and any(s.uses_prayer() for s in brand.slots.values()):
                t = PrayerCalculator(pcfg, tz).times(day)
                extra = f"  [fajr {t['fajr']:%H:%M}, maghrib {t['maghrib']:%H:%M}]"
            print(f"{day} {brand.id}{extra}")
            try:
                plans = plan_brand_day(app, brand, day, rng)
            except ScheduleError as exc:
                print(f"   ERROR: {exc}")
                continue
            for p in plans:
                when = p.target.astimezone(tz)
                next_day = " (+1d)" if when.date() != day else ""
                dl = f"  deadline {p.deadline.astimezone(tz):%H:%M}" if p.deadline else ""
                win = f"  window={p.window}" if p.window else ""
                streams = ",".join(brand.streams_for_slot(p.slot))
                print(f"   {p.slot:<10} {when:%H:%M:%S}{next_day}{dl}{win}  streams={streams}")
    print("\n(preview only: real targets are drawn once per day by the run and stored in state)")
    return 0


def commit_state(reason: str) -> None:
    script = ROOT / "scripts" / "commit_state.sh"
    result = subprocess.run(["bash", str(script), reason], cwd=ROOT, capture_output=True, text=True)
    if result.returncode != 0:
        log.error("state commit failed: %s", (result.stderr or result.stdout)[-500:])


def cmd_run(args, app: AppConfig) -> int:
    scheduled = os.environ.get("GITHUB_EVENT_NAME") == "schedule" or os.environ.get("AUTOPOST_TRIGGER") == "cron"
    if scheduled and not app.settings.cron_enabled:
        log.info("cron_enabled is false in config/settings.yaml: scheduled run does nothing")
        return 0
    dry = args.dry_run
    if not dry:
        print("Scheduled publishing needs Buffer (phase 4). For a single real post use: test-post", file=sys.stderr)
        return 2

    secrets = Secrets()
    # Dry runs work on a throwaway copy of the state so nothing they do is ever committed.
    state, tmpdir = open_state(args.state, throwaway=dry)
    now = parse_now(args.now, app)
    if dry and not args.real_sleep:
        clock = SimClock(now or datetime.now(app.settings.tz))
    else:
        if now:
            print("--now only works in dry-run without --real-sleep", file=sys.stderr)
            return 2
        clock = RealClock(app.settings.tz)
    use_clock_for_logs(app.settings.tz, clock.now if clock.simulated else None)
    notifier = make_notifier(app, secrets, dry_run=dry)
    content = make_content(app, state, secrets, dry, placeholder=args.placeholder_content,
                           notify=lambda text, key: notifier.alert(text, key=key, state=state, now=clock.now()))
    dry_pub = DryRunPublisher(download=args.download)
    runner = Runner(
        app, state, clock, notifier, content,
        publishers=lambda platform, provider: dry_pub,
        secrets=secrets,
        dry_run=dry,
        brand_filter=args.brand or None,
        checkpoint=commit_state if os.environ.get("AUTOPOST_COMMIT_STATE") == "1" else None,
        max_runtime_minutes=args.max_runtime_minutes,
        prefetch_media=not dry or args.download,
    )
    try:
        report = runner.run()
    finally:
        state.close()
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)
    print(report.summary())
    return 0


def cmd_sources(args, app: AppConfig) -> int:
    """Read-only preview of what each stream would publish next."""
    secrets = Secrets()
    state, tmpdir = open_state(args.state, throwaway=True)
    try:
        content = make_content(app, state, secrets, dry_run=True)
        if not isinstance(content, DriveContent):
            print("Google credentials missing; nothing to show", file=sys.stderr)
            return 1
        problems = 0
        for brand in app.enabled_brands():
            if args.brand and brand.id != args.brand:
                continue
            print(f"\n=== {brand.id} ({brand.name}) ===")
            src = brand.text_source
            if getattr(src, "type", "") == "sheet":
                try:
                    can_edit = content.client.can_edit(src.sheet_id)
                    rows = content.sheet_rows(brand.id)
                    usable = [r for r in rows if all(r.values.values())]
                    last = max((r for r in rows if r.posted_at), key=lambda r: (r.posted_at, r.number), default=None)
                    print(f"sheet tab {content.sheet_tab(brand.id)}: {len(rows)} data rows, {len(usable)} usable, "
                          f"service account can edit: {'yes' if can_edit else 'NO'}"
                          + (f", last posted row {last.number} at {last.posted_at:%Y-%m-%d %H:%M}" if last else ""))
                    if not can_edit:
                        problems += 1
                except (ContentError, GoogleError) as exc:
                    print(f"  ✗ sheet: {exc}")
                    problems += 1
            for stream_name, stream in brand.streams.items():
                try:
                    listing = content.listing(brand.id, stream_name)
                    unused = content.unused(brand.id, stream_name)
                except ContentError as exc:
                    print(f"  ✗ {stream_name}: {exc}")
                    problems += 1
                    continue
                per_day = len(stream.slots)
                print(f"-- stream {stream_name}: {len(listing)} videos, {len(listing) - len(unused)} used, "
                      f"{len(unused)} left ≈ {len(unused) / per_day:.1f} days at {per_day}/day")
                cursor = state.caption_cursor(brand.id)
                for i, video in enumerate(unused[: args.count]):
                    info = content.probe(video)
                    created = video.created.astimezone(app.settings.tz).strftime("%Y-%m-%d %H:%M") if video.created else "?"
                    print(f"  {i + 1}. {video.name}  [created {created}, {video.size or 0:,} B, "
                          f"{info.describe() if info else 'no media metadata yet'}]")
                    try:
                        if getattr(src, "type", "") == "sheet":
                            row = choose_next_row(content.sheet_rows(brand.id), cursor)
                            cursor = (row.number, datetime.now(app.settings.tz) + timedelta(days=i + 1))
                            variables, where = row.values, f"sheet row {row.number}"
                        else:
                            variables, where = content.pick_text(brand.id, stream_name, video).variables, "file name"
                    except ContentError as exc:
                        print(f"     ✗ text: {exc}")
                        problems += 1
                        continue
                    for platform in stream.platforms:
                        texts = render(brand.templates[platform], platform, variables, app.settings.text_limits)
                        shown = "  ".join(f"{k}[{len(v)}]: {v[:70].replace(chr(10), ' ')}{'…' if len(v) > 70 else ''}"
                                          for k, v in texts.items())
                        print(f"     {platform:<9} ({where}) {shown}")
        return 1 if problems else 0
    finally:
        state.close()
        shutil.rmtree(tmpdir, ignore_errors=True)


def cmd_check(args, app: AppConfig) -> int:
    """Read-only checks of the credentials each enabled platform needs (nothing is posted)."""
    secrets = Secrets()
    problems = 0
    notifier = make_notifier(app, secrets, dry_run=False)
    bot = notifier.get_me()
    print(f"{'✓' if bot else '✗'} Telegram bot {('@' + bot) if bot else 'token missing or invalid'}")
    problems += not bot
    if bot and args.ping_telegram:
        ok = notifier.send("✅ autopost check: Telegram works.")
        print(f"{'✓' if ok else '✗'} Telegram test message to the chat")
        problems += not ok
    version = app.settings.meta.graph_version
    for brand in app.enabled_brands():
        print(f"\n=== {brand.id} ===")
        seen = set()
        for stream in brand.streams:
            for platform in brand.enabled_platforms(stream):
                cfg = brand.platform_for(stream, platform)
                if platform in ("facebook", "instagram") and cfg.provider == "direct":
                    key = (cfg.token_secret, cfg.page_id if platform == "facebook" else cfg.ig_user_id)
                    if key in seen:
                        continue
                    seen.add(key)
                    if not secrets.has(cfg.token_secret):
                        print(f"✗ {platform}: secret {cfg.token_secret} is missing")
                        problems += 1
                        continue
                    from .platforms.meta import check_meta_access

                    for line in check_meta_access(secrets.get(cfg.token_secret), version,
                                                  cfg.page_id if platform == "facebook" else None,
                                                  cfg.ig_user_id if platform == "instagram" else None):
                        print(line)
                        problems += line.startswith("✗")
                elif cfg.provider == "buffer":
                    name = app.settings.buffer_accounts[cfg.buffer_account].api_key_secret
                    print(f"{'✓' if secrets.has(name) else '✗'} {platform} via Buffer ({cfg.buffer_account}): "
                          f"secret {name} {'present' if secrets.has(name) else 'missing'} (API check in phase 4)")
                    problems += not secrets.has(name)
                elif platform == "youtube":
                    mode = "Data API upload" if app.settings.youtube_direct.enabled else "manual hand-off on Telegram"
                    print(f"✓ youtube direct ({stream}): {mode}")
                elif platform == "pinterest":
                    names = [cfg.access_token_secret] if cfg.access_token_secret else [
                        app.settings.pinterest.app_id_secret, app.settings.pinterest.app_secret_secret,
                        cfg.refresh_token_secret]
                    missing = [n for n in names if not secrets.has(n)]
                    print(f"{'✗' if missing else '✓'} pinterest ({app.settings.pinterest.environment}): "
                          + (f"missing {', '.join(missing)}" if missing else "secrets present"))
                    problems += bool(missing)
    return 1 if problems else 0


def cmd_test_post(args, app: AppConfig) -> int:
    """Publish one real post now, to one or more platforms, with the same video and texts.

    Successful deliveries are recorded in state (early_deliveries): when the video's normal turn
    comes, those platforms are skipped and only the missing ones are posted. Nothing is written
    to the sheet and the video is not marked as used.
    """
    import time as _time

    from .platforms.base import PublishContext, Services
    from .platforms.registry import PublisherRegistry
    from .state import DeliveryRow

    brand = app.brands.get(args.brand)
    if brand is None:
        print(f"unknown brand {args.brand!r}", file=sys.stderr)
        return 2
    platforms = [p.strip() for p in args.platform.split(",") if p.strip()]
    stream = args.stream or next((s for s, cfg in brand.streams.items() if platforms[0] in cfg.platforms), None)
    if stream not in brand.streams:
        print(f"unknown stream {stream!r} for {brand.id}", file=sys.stderr)
        return 2
    for platform in platforms:
        if platform not in brand.streams[stream].platforms:
            print(f"{brand.id}/{stream} does not publish to {platform}", file=sys.stderr)
            return 2
    secrets = Secrets()
    state = State(args.state)
    notifier = make_notifier(app, secrets, dry_run=False)
    content = make_content(app, state, secrets, dry_run=True)   # dry_run here = never write the sheet
    if not isinstance(content, DriveContent):
        print("Google credentials missing", file=sys.stderr)
        return 1
    try:
        unused = content.unused(brand.id, stream)
        if args.video == "last":
            video = unused[-1] if unused else None
        elif args.video:
            video = next((v for v in content.listing(brand.id, stream) if args.video in (v.id, v.name)), None)
        else:
            video = unused[0] if unused else None
        if video is None:
            print(f"no matching unposted video in {brand.id}/{stream}", file=sys.stderr)
            return 2
        if video.id in state.used_source_ids(brand.id, stream):
            print(f"{video.name} was already posted by the schedule; pick another video", file=sys.stderr)
            return 2
        early = {r["platform"]: r for r in state.early_deliveries(brand.id, stream, video.id)}
        if early:
            variables = json.loads(next(iter(early.values()))["variables_json"])
        else:
            variables = content.pick_text(brand.id, stream, video).variables
        clock = RealClock(app.settings.tz)
        services = Services(app=app, state=state, notifier=notifier, secrets=secrets, content=content,
                            now=clock.now, advance=clock.advance, dry_run=False)
        registry = PublisherRegistry(services)
        print(f"TEST POST {brand.id}/{stream}: {video.name}")
        results = []
        for platform in platforms:
            if platform in early:
                print(f"- {platform}: already delivered earlier ({early[platform]['url'] or early[platform]['status']}); skipped")
                continue
            cfg = brand.platform_for(stream, platform)
            texts = render(brand.templates[platform], platform, variables, app.settings.text_limits)
            ctx = PublishContext(brand=brand, stream=stream, platform=platform, cfg=cfg, post_id=0, video=video,
                                 texts=texts, deadline=None, services=services,
                                 fetch_video=lambda: content.fetch(video), media_info=lambda: content.probe(video))
            print(f"- {platform} via {cfg.provider}: " + " | ".join(f"{k}[{len(v)}]" for k, v in texts.items()))
            publisher = registry(platform, cfg.provider)
            outcome = publisher.publish(ctx)
            give_up = _time.monotonic() + 30 * 60
            while outcome.status == "checking" and _time.monotonic() < give_up:
                wait = max(5, (outcome.check_after - clock.now()).total_seconds()) if outcome.check_after else 15
                log.info("waiting %.0fs for %s to finish processing…", wait, platform)
                _time.sleep(wait)
                outcome = publisher.check(ctx, DeliveryRow(0, 0, platform, cfg.provider, 0, "checking", 1,
                                                           outcome.external_id, None, None, None))
            line = f"{platform}: {outcome.status}" + (f" {outcome.url}" if outcome.url else "") + (
                f" — {outcome.error}" if outcome.error else "")
            print(f"  RESULT {line}")
            results.append(line)
            if outcome.status in ("sent", "manual"):
                state.record_early_delivery(brand.id, stream, video.id, platform, outcome.provider or cfg.provider,
                                            outcome.status, outcome.external_id, outcome.url, variables, clock.now())
        content.release(video)
        if results:
            notifier.send(f"🧪 Test post — {brand.name}/{stream}\n{video.name}\n\n" + "\n".join(results))
        return 0 if all(": sent" in r or ": manual" in r for r in results) else 1
    finally:
        state.close()


def cmd_status(args, app: AppConfig) -> int:
    state = State(args.state)
    tz = app.settings.tz
    today = datetime.now(tz).date()
    print("slots (yesterday → tomorrow):")
    for s in state.slots(days=[today - timedelta(days=1), today, today + timedelta(days=1)]):
        dl = f" deadline {s.deadline.astimezone(tz):%H:%M}" if s.deadline else ""
        print(f"  {s.slot_date} {s.brand:<14} {s.slot:<10} {s.target.astimezone(tz):%m-%d %H:%M}{dl}"
              f"  {s.status}{'  (' + s.note + ')' if s.note else ''}")
    print("recent posts:")
    for p in state.recent_posts(10):
        ds = ", ".join(f"{d.platform}:{d.status}" for d in state.deliveries(p["id"]))
        print(f"  #{p['id']} {p['slot_date']} {p['brand']}/{p['stream']}/{p['slot']} {p['source_name']} "
              f"[{p['status']}] {ds}")
    now = datetime.now(tz)
    for acct in app.settings.buffer_accounts:
        print(f"buffer {acct}: {state.buffer_requests_since(acct, now - timedelta(days=1))} requests in 24h")
    state.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="autopost")
    parser.add_argument("--config", default=str(ROOT / "config"))
    parser.add_argument("--state", default=str(ROOT / "state" / "state.db"))
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("validate", help="check config (and optionally secrets / Telegram)")
    p.add_argument("--check-secrets", action="store_true")
    p.add_argument("--ping-telegram", action="store_true")
    p.add_argument("--strict", action="store_true", help="exit 1 on placeholders / missing secrets")

    p = sub.add_parser("plan", help="preview slot targets (not stored)")
    p.add_argument("--date")
    p.add_argument("--days", type=int, default=3)
    p.add_argument("--brand")
    p.add_argument("--seed", type=int)

    p = sub.add_parser("run", help="publish due slots")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--brand")
    p.add_argument("--now", help="dry-run only: pretend it is this local time (YYYY-MM-DDTHH:MM)")
    p.add_argument("--real-sleep", action="store_true", help="dry-run only: really wait instead of simulating")
    p.add_argument("--max-runtime-minutes", type=int, default=120)
    p.add_argument("--download", action="store_true", help="dry-run: also download each video and ffprobe it")
    p.add_argument("--placeholder-content", action="store_true", help="dry-run: fake videos instead of Drive")

    p = sub.add_parser("check", help="read-only credential checks (Telegram, Meta, Buffer/Pinterest secrets)")
    p.add_argument("--ping-telegram", action="store_true", help="also send a test message")

    p = sub.add_parser("test-post", help="publish ONE real post to ONE platform now (not recorded)")
    p.add_argument("--brand", required=True)
    p.add_argument("--platform", required=True, help="one or more, comma separated: facebook,instagram")
    p.add_argument("--stream", help="default: the first stream of the brand that publishes to the platform")
    p.add_argument("--video", help="'last' (last unposted in the folder order), a file name or a Drive id "
                                   "(default: the next unposted video)")

    p = sub.add_parser("sources", help="read-only preview of Drive videos and sheet texts")
    p.add_argument("--brand")
    p.add_argument("--count", type=int, default=3, help="videos to preview per stream")

    sub.add_parser("status", help="show state")

    args = parser.parse_args(argv)
    setup_logging(args.verbose)
    try:
        app = load_config(args.config)
    except ConfigError as exc:
        print(f"CONFIG ERROR\n{exc}", file=sys.stderr)
        return 1
    use_clock_for_logs(app.settings.tz)
    try:
        return {"validate": cmd_validate, "plan": cmd_plan, "run": cmd_run, "sources": cmd_sources,
                "check": cmd_check, "test-post": cmd_test_post, "status": cmd_status}[args.command](
            args, app)
    except (MissingSecret, GoogleError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
