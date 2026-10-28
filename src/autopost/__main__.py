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
from .content.drive_source import ContentError, DriveContent, choose_next_row, format_ranges, numbering_report
from .content.google_api import GoogleClient, GoogleError, session_from_service_account
from .content.text import render
from .platforms.base import DryRunPublisher
from .media import ensure_ffmpeg
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
    """Real runs report to healthchecks.io: /start now, then success or /fail (never blocking the run)."""
    if args.dry_run:
        return _run(args, app)[0]
    import traceback

    from .healthcheck import Healthcheck

    secrets = Secrets()
    name = app.settings.healthcheck.ping_url_secret
    hc = Healthcheck(secrets.get(name) if secrets.has(name) else None)
    hc.start()
    try:
        code, summary = _run(args, app)
    except BaseException as exc:
        hc.fail(f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[-3000:]}")
        raise
    (hc.success if code == 0 else hc.fail)(summary)
    return code


def _run(args, app: AppConfig) -> tuple[int, str]:
    scheduled = os.environ.get("GITHUB_EVENT_NAME") == "schedule" or os.environ.get("AUTOPOST_TRIGGER") == "cron"
    dry = args.dry_run
    if not dry:
        maintenance(app, args.state)
    if scheduled and not app.settings.cron_enabled:
        log.info("cron_enabled is false in config/settings.yaml: scheduled run publishes nothing")
        return 0, "cron_enabled is false: housekeeping only"

    secrets = Secrets()
    # Dry runs work on a throwaway copy of the state so nothing they do is ever committed.
    state, tmpdir = open_state(args.state, throwaway=dry)
    now = parse_now(args.now, app)
    if dry and not args.real_sleep:
        clock = SimClock(now or datetime.now(app.settings.tz))
    else:
        if now:
            print("--now only works in dry-run without --real-sleep", file=sys.stderr)
            return 2, "bad arguments"
        clock = RealClock(app.settings.tz)
    use_clock_for_logs(app.settings.tz, clock.now if clock.simulated else None)
    notifier = make_notifier(app, secrets, dry_run=dry)
    content = make_content(app, state, secrets, dry, placeholder=args.placeholder_content,
                           notify=lambda text, key: notifier.alert(text, key=key, state=state, now=clock.now()))
    dry_pub = DryRunPublisher(download=args.download)
    runner = Runner(
        app, state, clock, notifier, content,
        publishers=(lambda platform, provider: dry_pub) if dry else None,
        secrets=secrets,
        dry_run=dry,
        brand_filter=args.brand or None,
        checkpoint=commit_state if os.environ.get("AUTOPOST_COMMIT_STATE") == "1" else None,
        max_runtime_minutes=args.max_runtime_minutes,
        prefetch_media=not dry or args.download,
        ensure_tools=(lambda: ensure_ffmpeg(alert=lambda text: notifier.alert(text))) if (not dry or args.download) else None,
    )
    try:
        report = runner.run()
    finally:
        state.close()
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)
    print(report.summary())
    return 0, report.summary()


def maintenance(app: AppConfig, state_path: str | Path) -> None:
    """Housekeeping on every real run, even when publishing is switched off: hide Facebook comments
    for brands with `comments: off`, and remove stale temporary videos from the media-temp release."""
    from .hosting.github_release import GitHubReleaseHost
    from .platforms.meta import hide_facebook_comments

    secrets = Secrets()
    state = State(state_path)
    try:
        notifier = make_notifier(app, secrets, dry_run=False)
        try:
            hide_facebook_comments(app, state, secrets, notifier, datetime.now(app.settings.tz))
        except Exception:  # noqa: BLE001 - housekeeping must never stop publishing
            log.exception("facebook comment hiding failed")
        if app.settings.cron_enabled:
            try:
                from .summary import send_daily_summary

                content = make_content(app, state, secrets, dry_run=True)   # read-only use
                send_daily_summary(app, state, content, notifier, datetime.now(app.settings.tz))
            except Exception:  # noqa: BLE001
                log.exception("daily summary failed")
        host = GitHubReleaseHost.from_env(app.settings.media_hosting.github_release_tag)
        if host is not None:
            try:
                host.cleanup()
            except Exception:  # noqa: BLE001
                log.exception("media-temp cleanup failed")
    finally:
        state.close()


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
                used_ids = state.used_source_ids(brand.id, stream_name)
                used = sum(1 for v in listing if v.id in used_ids)
                skipped = [v.name for v in listing if v.id not in used_ids and content.skipped_title(brand.id, stream_name, v)]
                print(f"-- stream {stream_name}: {len(listing)} videos, {used} used, "
                      f"{len(unused)} left ≈ {len(unused) / per_day:.1f} days at {per_day}/day"
                      + (f"; skipped (no caption): {', '.join(skipped)}" if skipped else ""))
                tg = sum(1 for v in listing if v.app_properties.get("source") == "tg2drive")
                if tg:
                    print(f"   tg2drive files: {tg}")
                stats = content.stream_stats(brand.id, stream_name, datetime.now(app.settings.tz).date())
                if stats is not None:
                    print(f"   backfill: {'complete' if stats['backfill_done'] else 'in progress'} "
                          f"({stats['in_folder']}/{stats['backfill_size']} Telegram reels in the folder); "
                          f"queue {stats['queue']} ({stats['queue_new']} new); received today {stats['received']}")
                groups = [("", unused)]
                cursor = state.caption_cursor(brand.id)
                shown_videos = [(label, i, v) for label, vids in groups for i, v in enumerate(vids[: args.count])]
                for label, i, video in shown_videos:
                    info = content.probe(video)
                    created = video.created.astimezone(app.settings.tz).strftime("%Y-%m-%d %H:%M") if video.created else "?"
                    msg = f", msg {video.tg_msg_id}" if video.tg_msg_id is not None else ""
                    print(f"  {label}{i + 1}. {video.name}  [created {created}{msg}, {video.size or 0:,} B, "
                          f"{info.describe() if info else 'no media metadata yet'}]")
                    try:
                        if getattr(src, "type", "") == "sheet":
                            row = choose_next_row(content.sheet_rows(brand.id), cursor, src.start_row)
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
    """Read-only checks of everything the bot needs (nothing is posted)."""
    from .platforms.buffer import BufferClient, BufferError, channel_matches
    from .platforms.meta import check_meta_access, check_token_scopes

    secrets = Secrets()
    problems = 0

    def show(line: str) -> None:
        nonlocal problems
        print(line)
        problems += line.lstrip().startswith("✗")

    print("=== Telegram ===")
    notifier = make_notifier(app, secrets, dry_run=False)
    bot = notifier.get_me()
    show(f"{'✓' if bot else '✗'} bot {('@' + bot) if bot else 'token missing or invalid'}")
    if bot and args.ping_telegram:
        show(f"{'✓' if notifier.send('✅ autopost check: Telegram works.') else '✗'} test message to the chat")

    print("\n=== Google Drive / Sheets ===")
    state, tmpdir = open_state(args.state, throwaway=True)
    try:
        content = make_content(app, state, secrets, dry_run=True)
        if not isinstance(content, DriveContent):
            show(f"✗ {app.settings.google.service_account_secret} missing")
        else:
            for brand in app.enabled_brands():
                for stream in brand.streams:
                    try:
                        show(f"✓ {brand.id}/{stream}: {len(content.listing(brand.id, stream))} videos")
                    except ContentError as exc:
                        show(f"✗ {brand.id}/{stream}: {exc}")
                src = brand.text_source
                if getattr(src, "type", "") == "sheet":
                    try:
                        rows = content.sheet_rows(brand.id)
                        can_edit = content.client.can_edit(src.sheet_id)
                        show(f"{'✓' if can_edit else '✗'} {brand.id} sheet {content.sheet_tab(brand.id)}: "
                             f"{len(rows)} rows, service account can edit: {'yes' if can_edit else 'NO'}")
                    except (ContentError, GoogleError) as exc:
                        show(f"✗ {brand.id} sheet: {exc}")
    finally:
        state.close()
        shutil.rmtree(tmpdir, ignore_errors=True)

    version = app.settings.meta.graph_version
    for brand in app.enabled_brands():
        print(f"\n=== Meta: {brand.id} ===")
        tokens_seen = set()
        for platform in ("facebook", "instagram"):
            cfg = brand.platforms.get(platform)
            if cfg is None or cfg.provider != "direct" or not cfg.enabled:
                continue
            if not secrets.has(cfg.token_secret):
                show(f"✗ {platform}: secret {cfg.token_secret} is missing")
                continue
            token = secrets.get(cfg.token_secret)
            if cfg.token_secret not in tokens_seen:
                tokens_seen.add(cfg.token_secret)
                extra = ["instagram_manage_comments", "pages_read_user_content", "pages_manage_engagement"] \
                    if brand.comments == "off" else []
                print(f"{cfg.token_secret}:")
                for line in check_token_scopes(token, version, extra):
                    show("  " + line)
            for line in check_meta_access(token, version, cfg.page_id if platform == "facebook" else None,
                                          cfg.ig_user_id if platform == "instagram" else None):
                show(line)

    print("\n=== Buffer ===")
    for name, acct in app.settings.buffer_accounts.items():
        wanted = sorted({brand.platform_for(s, p).channel for brand in app.enabled_brands() for s in brand.streams
                         for p in brand.enabled_platforms(s)
                         if brand.platform_for(s, p).provider == "buffer"
                         and brand.platform_for(s, p).buffer_account == name})
        if not secrets.has(acct.api_key_secret):
            show(f"✗ {name}: secret {acct.api_key_secret} is missing")
            continue
        client = BufferClient(secrets.get(acct.api_key_secret))
        try:
            account = client.account()
            orgs = account.get("organizations") or []
            show(f"✓ {name} ({acct.api_key_secret}): key works, {len(orgs)} organization(s)")
            channels = [c for org in orgs for c in client.channels(org["id"])]
        except BufferError as exc:
            show(f"✗ {name} ({acct.api_key_secret}): {exc}")
            continue
        for c in channels:
            flags = (" DISCONNECTED" if c.get("isDisconnected") else "") + (" LOCKED" if c.get("isLocked") else "")
            print(f"   - {c['service']}: name={c.get('name')!r} display={c.get('displayName')!r}{flags}")
        for want in wanted:
            match = [c for c in channels if channel_matches(c, want)]
            show(f"{'✓' if match else '✗'} configured channel {want!r} "
                 + (f"→ {match[0]['service']} {match[0]['id']}" if match else "not found in this account"))
    print(f"\n{'all checks passed' if not problems else f'{problems} problem(s)'}")
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
        ensure_ffmpeg(alert=lambda text: notifier.alert(text))
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


def cmd_meta_info(args, app: AppConfig) -> int:
    """Read-only: show what Facebook/Instagram report for a published video or media id."""
    from .platforms.meta import GraphClient, GraphError

    brand = app.brands[args.brand]
    platform = "facebook" if args.platform == "facebook" else "instagram"
    cfg = brand.platforms[platform]
    g = GraphClient(Secrets().get(cfg.token_secret), app.settings.meta.graph_version)
    fields = ("id,length,permalink_url,published,status,title,description,created_time" if platform == "facebook"
              else "id,media_type,media_product_type,permalink,is_comment_enabled,comments_count,timestamp")
    try:
        data = g.call("GET", args.id, params={"fields": fields})
    except GraphError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


def cmd_merge_state(args, app: AppConfig) -> int:
    """Merge two state DBs (used by scripts/commit_state.sh on a push conflict)."""
    from .state import merge_states

    added = merge_states(args.theirs, args.ours, args.out)
    print("merged state: " + (", ".join(f"{k}+{v}" for k, v in sorted(added.items())) or "no differences"))
    return 0


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

    p = sub.add_parser("meta-info", help="read-only: what Facebook/Instagram report for a published id")
    p.add_argument("--brand", required=True)
    p.add_argument("--platform", required=True, choices=["facebook", "instagram"])
    p.add_argument("--id", required=True)

    p = sub.add_parser("sources", help="read-only preview of Drive videos and sheet texts")
    p.add_argument("--brand")
    p.add_argument("--count", type=int, default=3, help="videos to preview per stream")

    sub.add_parser("status", help="show state")

    p = sub.add_parser("merge-state", help="merge two state DBs (push-conflict resolution)")
    p.add_argument("--theirs", required=True)
    p.add_argument("--ours", required=True)
    p.add_argument("--out", required=True)

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
                "check": cmd_check, "test-post": cmd_test_post, "meta-info": cmd_meta_info, "merge-state": cmd_merge_state, "status": cmd_status}[args.command](
            args, app)
    except (MissingSecret, GoogleError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
