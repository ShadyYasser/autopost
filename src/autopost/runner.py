"""One publishing run.

1. Make sure today's (and neighbouring days') slot targets exist in state.
2. Collect slots whose target is within the lookahead window, plus unfinished posts
   from earlier runs (retries, crashed runs, pending Buffer checks).
3. Event loop: sleep until the next thing is due, start slots at their target, publish
   one platform at a time per post with random gaps, poll pending deliveries.
   Hard deadlines (prayer slots) take priority and are never overrun.
"""

from __future__ import annotations

import json
import logging
import random
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Callable

from .alerts.telegram import Notifier
from .config import AppConfig, BrandConfig
from .content.base import ContentSource, TextChoice, VideoItem
from .content.text import render
from .platforms.base import Outcome, Publisher, PublishContext, Services
from .scheduler import ScheduleError, SlotPlan, plan_brand_day, slots_fingerprint
from .secret_store import Secrets
from .state import FINAL_DELIVERY, DeliveryRow, SlotRow, State

log = logging.getLogger(__name__)

PublisherFactory = Callable[[str, str], Publisher]

# Deadline posts: assumed blocking time of one platform publish, and the smallest gap we shrink to.
EST_PUBLISH_SECONDS = 30
MIN_DEADLINE_GAP_SECONDS = 10


@dataclass
class Chain:
    """The deliveries of one post, published one after another."""

    brand: BrandConfig
    stream: str
    slot: SlotRow
    post_id: int
    video: VideoItem
    variables: dict[str, str]
    queue: deque[int]
    ready_at: datetime
    gap: tuple[int, int]
    outstanding_checks: int = 0
    media: object | None = None
    skipped_deadline: list[str] = field(default_factory=list)

    @property
    def finished(self) -> bool:
        return not self.queue and self.outstanding_checks == 0


@dataclass
class Check:
    chain: Chain
    delivery_id: int
    ready_at: datetime


@dataclass
class RunReport:
    published: list[str] = field(default_factory=list)
    manual: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (f"published={len(self.published)} manual={len(self.manual)} "
                f"failed={len(self.failed)} skipped={len(self.skipped)}")


class Runner:
    def __init__(self, app: AppConfig, state: State, clock, notifier: Notifier, content: ContentSource,
                 publishers: PublisherFactory, secrets: Secrets, rng: random.Random | None = None,
                 dry_run: bool = False, brand_filter: str | None = None,
                 checkpoint: Callable[[str], None] | None = None, max_runtime_minutes: int = 120,
                 prefetch_media: bool = False, ensure_tools: Callable[[], None] | None = None):
        self.app = app
        self.settings = app.settings
        self.tz = app.settings.tz
        self.state = state
        self.clock = clock
        self.notifier = notifier
        self.content = content
        self.publishers = publishers
        self.rng = rng or random.SystemRandom()
        self.dry_run = dry_run
        self.brand_filter = brand_filter
        self._checkpoint = checkpoint
        self.max_runtime = timedelta(minutes=max_runtime_minutes)
        self.prefetch_media = prefetch_media
        self._ensure_tools = ensure_tools
        self._tools_ready = False
        self._prefetched: set[tuple] = set()
        self.report = RunReport()
        self.services = Services(app=app, state=state, notifier=notifier, secrets=secrets, content=content,
                                 now=clock.now, advance=clock.advance, dry_run=dry_run)
        if publishers is None:
            from .platforms.registry import PublisherRegistry

            publishers = PublisherRegistry(self.services)
            self.publishers = publishers
        if brand_filter and brand_filter not in app.brands:
            raise ValueError(f"unknown brand {brand_filter!r}")

    # ------------------------------------------------------------ helpers

    def brands(self) -> list[BrandConfig]:
        return [b for b in self.app.enabled_brands() if not self.brand_filter or b.id == self.brand_filter]

    def local_today(self, now: datetime) -> date:
        return now.astimezone(self.tz).date()

    def margin(self) -> timedelta:
        return timedelta(seconds=self.settings.scheduler.deadline_margin_seconds)

    def lateness(self, brand: BrandConfig, slot: str) -> timedelta:
        cfg = brand.slots[slot].max_lateness_minutes
        return timedelta(minutes=cfg or self.settings.scheduler.max_lateness_minutes)

    def gap_for(self, brand: BrandConfig, slot: str) -> tuple[int, int]:
        return brand.slots[slot].gap_seconds or self.settings.scheduler.platform_gap_seconds

    def random_gap(self, gap: tuple[int, int]) -> timedelta:
        return timedelta(seconds=round(self.rng.uniform(*gap)))

    def fmt(self, dt: datetime | None) -> str:
        return "-" if dt is None else dt.astimezone(self.tz).strftime("%Y-%m-%d %H:%M:%S")

    def checkpoint(self, reason: str) -> None:
        if self._checkpoint and not self.dry_run:
            self._checkpoint(reason)

    def too_late(self, brand: BrandConfig, slot: str, target: datetime, deadline: datetime | None,
                 now: datetime) -> str | None:
        if deadline is not None and now >= deadline - self.margin():
            return f"reached at {self.fmt(now)}, deadline {self.fmt(deadline)}"
        if deadline is None and now > target + self.lateness(brand, slot):
            return f"reached at {self.fmt(now)}, target was {self.fmt(target)}"
        return None

    # ------------------------------------------------------------ slots

    def ensure_slots(self, now: datetime) -> None:
        today = self.local_today(now)
        for brand in self.brands():
            fp_key = f"slots-config:{brand.id}"
            fingerprint = slots_fingerprint(self.app, brand)
            stored = self.state.kv_get(fp_key)
            for day in (today - timedelta(days=1), today, today + timedelta(days=1)):
                if self.state.has_slots(brand.id, day):
                    if day >= today and stored != fingerprint:
                        names = {s.slot for s in self.state.slots(brand=brand.id, days=[day])}
                        # No fingerprint yet (first run with this check): only a visible change counts.
                        if stored is not None or names != set(brand.slots):
                            self.replan_day(brand, day, now)
                    continue
                try:
                    plans = plan_brand_day(self.app, brand, day, self.rng)
                except ScheduleError as exc:
                    self.notifier.alert(f"{brand.name}: could not compute slots for {day}: {exc}",
                                        key=f"schedule-error:{brand.id}:{day}", state=self.state, now=now)
                    continue
                for p in plans:
                    late = self.too_late(brand, p.slot, p.target, p.deadline, now)
                    self.state.insert_slot(brand.id, day, p.slot, p.target, p.deadline, p.window, now,
                                           status="skipped" if late else "scheduled",
                                           note="created after its time" if late else None)
                    log.info("%s %s %-8s target %s%s%s", brand.id, day, p.slot, self.fmt(p.target),
                             f" (window {p.window})" if p.window else "",
                             f" deadline {self.fmt(p.deadline)}" if p.deadline else "")

            if stored != fingerprint:
                self.state.kv_set(fp_key, fingerprint)

    def replan_day(self, brand: BrandConfig, day: date, now: datetime) -> None:
        """The brand's slot config changed: re-draw the day's slots that have not started yet.
        Started, finished and skipped slots keep their time; slots no longer in the config are dropped."""
        existing = {s.slot: s for s in self.state.slots(brand=brand.id, days=[day])}
        fixed = {name: SlotPlan(brand.id, day, name, s.target, s.deadline, s.window)
                 for name, s in existing.items() if s.status != "scheduled"}
        try:
            plans = plan_brand_day(self.app, brand, day, self.rng, fixed=fixed)
        except ScheduleError as exc:
            self.notifier.alert(f"{brand.name}: could not re-plan slots for {day}: {exc}",
                                key=f"schedule-error:{brand.id}:{day}", state=self.state, now=now)
            return
        names = {p.slot for p in plans}
        for name, s in existing.items():
            if s.status == "scheduled" and name not in names:
                self.state.set_slot_status(brand.id, day, name, "skipped", now, note="removed from the schedule")
                log.info("%s %s %-9s removed from the schedule", brand.id, day, name)
        for p in plans:
            if p.slot in fixed:
                continue
            late = self.too_late(brand, p.slot, p.target, p.deadline, now)
            status, note = ("skipped", "created after its time") if late else ("scheduled", None)
            if p.slot in existing:
                self.state.update_slot_target(brand.id, day, p.slot, p.target, p.deadline, p.window, now,
                                              status=status, note=note)
            else:
                self.state.insert_slot(brand.id, day, p.slot, p.target, p.deadline, p.window, now,
                                       status=status, note=note)
            log.info("%s %s %-9s re-planned: target %s%s", brand.id, day, p.slot, self.fmt(p.target),
                     " (already past: skipped)" if late else "")

    def due_slots(self, now: datetime) -> list[SlotRow]:
        today = self.local_today(now)
        horizon = now + timedelta(minutes=self.settings.scheduler.lookahead_minutes)
        brands = {b.id for b in self.brands()}
        rows = self.state.slots(days=[today - timedelta(days=1), today, today + timedelta(days=1)],
                                statuses=["scheduled"])
        return [r for r in rows if r.brand in brands and r.target <= horizon]

    def start_slot(self, slot: SlotRow, now: datetime) -> list[Chain]:
        brand = self.app.brands[slot.brand]
        label = f"{brand.name} / {slot.slot} ({slot.slot_date})"
        late = self.too_late(brand, slot.slot, slot.target, slot.deadline, now)
        if late:
            self.state.set_slot_status(brand.id, slot.slot_date, slot.slot, "skipped", now, note=late)
            self.report.skipped.append(label)
            self.notifier.alert(f"Slot skipped: {label}: {late}. No video was used.",
                                key=f"slot-late:{brand.id}:{slot.slot_date}:{slot.slot}", state=self.state, now=now)
            self.checkpoint(f"skip {brand.id} {slot.slot}")
            return []

        log.info("▶ starting %s (target %s)", label, self.fmt(slot.target))
        self.state.set_slot_status(brand.id, slot.slot_date, slot.slot, "in_progress", now)
        gap = self.gap_for(brand, slot.slot)
        chains: list[Chain] = []
        errors: list[str] = []
        for stream in brand.streams_for_slot(slot.slot):
            try:
                video = self.content.next_video(brand.id, stream, slot=slot.slot)
                if video is None:
                    self.notifier.alert(
                        f"{brand.name}: no unposted videos left in stream '{stream}'. Slot {slot.slot} skipped for it.",
                        key=f"empty:{brand.id}:{stream}:{self.local_today(now)}", state=self.state, now=now)
                    continue
                early = {r["platform"]: r for r in self.state.early_deliveries(brand.id, stream, video.id)}
                if early:
                    # Some platforms got this video in a test post: reuse its texts, consume no sheet row.
                    text = TextChoice(json.loads(next(iter(early.values()))["variables_json"]))
                else:
                    text = self.content.pick_text(brand.id, stream, video)
            except Exception as exc:  # noqa: BLE001 - Drive/Sheets trouble must not crash the run
                log.exception("%s/%s: content error", brand.id, stream)
                errors.append(f"{stream}: {exc}")
                continue
            platforms = brand.enabled_platforms(stream)
            if not platforms:
                log.warning("%s/%s: every platform is disabled; nothing to post", brand.id, stream)
                continue
            self.rng.shuffle(platforms)
            routing = [(p, brand.platform_for(stream, p).provider) for p in platforms]
            post_id = self.state.create_post(brand.id, stream, slot.slot_date, slot.slot, video.id, video.name,
                                             video.created, text.row, text.variables, routing, now)
            try:
                self.content.commit_text(brand.id, text, now)
            except Exception as exc:  # noqa: BLE001
                log.exception("commit_text failed")
                self.notifier.alert(f"{brand.name}: {exc}. Rotation continues from the position saved in state.")
            for d in self.state.deliveries(post_id):
                pre = early.get(d.platform)
                if pre is not None:
                    self.state.update_delivery(d.id, now, status=pre["status"], external_id=pre["external_id"],
                                               url=pre["url"], provider=pre["provider"],
                                               error=f"delivered early (test post {pre['delivered_utc']})")
            log.info("  post #%s %s/%s: %s, order %s%s", post_id, brand.id, stream, video.name,
                     " → ".join(p for p, _ in routing),
                     f" (already done: {', '.join(sorted(early))})" if early else "")
            ids = deque(d.id for d in self.state.deliveries(post_id) if d.status not in FINAL_DELIVERY)
            ready = now if not chains else now + self.random_gap(gap)
            chain = Chain(brand, stream, slot, post_id, video, text.variables, ids, ready, gap)
            if chain.finished:
                self.finish_chain(chain)
            else:
                chains.append(chain)
            try:
                self.warn_low_content(brand, stream, now)
            except Exception:  # noqa: BLE001
                log.exception("low-content check failed")
        if errors:
            self.notifier.alert(f"{label}: could not prepare content:\n" + "\n".join(errors)
                                + ("\nThe slot will be retried on the next run." if not chains else ""),
                                key=f"content-error:{brand.id}:{slot.slot_date}:{slot.slot}", state=self.state, now=now)
        if not chains and errors and not self.state.posts_for_slot(brand.id, slot.slot_date, slot.slot):
            # Nothing was used up: leave the slot for the next run (it is skipped there if too late).
            self.state.set_slot_status(brand.id, slot.slot_date, slot.slot, "scheduled", now)
            return []
        if not chains and not self.state.posts_for_slot(brand.id, slot.slot_date, slot.slot):
            self.state.set_slot_status(brand.id, slot.slot_date, slot.slot, "skipped", now, note="no content")
            self.report.skipped.append(label)
        self.checkpoint(f"start {brand.id} {slot.slot}")
        return chains

    def publishable(self, slot: SlotRow, now: datetime) -> bool:
        """Due slot that will really be published (not one about to be skipped as too late)."""
        return self.too_late(self.app.brands[slot.brand], slot.slot, slot.target, slot.deadline, now) is None

    def ensure_tools(self) -> None:
        """Install ffmpeg once, as soon as there is work in this run (before any download/transcode)."""
        if self._tools_ready or self._ensure_tools is None:
            return
        self._tools_ready = True
        try:
            self._ensure_tools()
        except Exception as exc:  # noqa: BLE001 - the slot then fails at prepare time and is retried/reported
            log.exception("ffmpeg setup failed")
            self.notifier.alert(f"ffmpeg could not be installed: {exc}",
                                key=f"ffmpeg-install:{self.local_today(self.clock.now())}", state=self.state,
                                now=self.clock.now())

    def prefetch(self, slot: SlotRow) -> None:
        """While waiting for a slot, download (and transcode) its next video so the target is not delayed."""
        key = (slot.brand, slot.slot_date, slot.slot)
        if not self.prefetch_media or key in self._prefetched:
            return
        self._prefetched.add(key)
        brand = self.app.brands[slot.brand]
        for stream in brand.streams_for_slot(slot.slot):
            try:
                video = self.content.next_video(brand.id, stream, slot=slot.slot)
                if video is not None:
                    log.info("preparing %s/%s ahead of %s: %s", brand.id, stream, self.fmt(slot.target), video.name)
                    self.content.fetch(video)
            except Exception as exc:  # noqa: BLE001 - it is retried when the slot starts
                log.warning("prefetch for %s/%s failed: %s", brand.id, stream, exc)

    def warn_low_content(self, brand: BrandConfig, stream: str, now: datetime) -> None:
        remaining = self.content.remaining(brand.id, stream)
        per_day = len(brand.streams[stream].slots)
        days = remaining / per_day
        if days <= self.settings.low_content_warning_days:
            self.notifier.alert(
                f"{brand.name}: only {remaining} unposted video(s) left in stream '{stream}' "
                f"(≈{days:.1f} day(s) at {per_day}/day). Please add more.",
                key=f"low:{brand.id}:{stream}:{self.local_today(now)}", state=self.state, now=now)

    # ------------------------------------------------------------ resume earlier work

    def resume(self, now: datetime) -> tuple[list[Chain], list[Check]]:
        chains: list[Chain] = []
        checks: list[Check] = []
        today = self.local_today(now)
        max_attempts = self.settings.max_attempts_per_delivery
        brands = {b.id: b for b in self.brands()}
        for day in (today - timedelta(days=1), today):
            for slot in self.state.slots(days=[day], statuses=["in_progress"]):
                brand = brands.get(slot.brand)
                if brand is None:
                    continue
                for post in self.state.posts_for_slot(slot.brand, slot.slot_date, slot.slot):
                    if post["status"] not in ("in_progress", "retrying"):
                        continue
                    open_ = [d for d in self.state.deliveries(post["id"])
                             if d.status not in FINAL_DELIVERY and not (d.status == "failed" and d.attempts >= max_attempts)]
                    if not open_:
                        continue
                    if slot.deadline is not None and now >= slot.deadline - self.margin():
                        for d in open_:
                            self.state.update_delivery(d.id, now, status="skipped_deadline", error="deadline passed")
                        self.finish_post(post["id"], slot, now)
                        continue
                    video = self.content.video_by_id(brand.id, post["stream"], post["source_id"])
                    if video is None:
                        log.error("post #%s: source video %s no longer available", post["id"], post["source_id"])
                        continue
                    variables = json.loads(post["variables_json"])
                    gap = self.gap_for(brand, slot.slot)
                    chain = Chain(brand, post["stream"], slot, post["id"], video, variables,
                                  deque(d.id for d in open_ if d.status != "checking"), now, gap)
                    for d in open_:
                        if d.status == "checking":
                            chain.outstanding_checks += 1
                            checks.append(Check(chain, d.id, d.check_after or now))
                    log.info("resuming post #%s (%s/%s): %d open deliveries", post["id"], brand.id,
                             post["stream"], len(open_))
                    chains.append(chain)
        return chains, checks

    # ------------------------------------------------------------ main loop

    def run(self) -> RunReport:
        started = self.clock.now()
        self.state.prune(started)
        self.ensure_slots(started)
        pending = deque(self.due_slots(started))
        chains, checks = self.resume(started)
        if pending:
            log.info("due within lookahead: %s", ", ".join(f"{s.brand}/{s.slot}@{self.fmt(s.target)}" for s in pending))
        else:
            log.info("no slot due within the next %d min", self.settings.scheduler.lookahead_minutes)

        known = {(s.brand, s.slot_date, s.slot) for s in pending}
        day = self.local_today(started)
        if chains or checks or any(self.publishable(r, started) for r in pending):
            self.ensure_tools()
        while True:
            now = self.clock.now()
            accepting = now - started < self.max_runtime
            if accepting and (chains or checks or pending):
                # While this run is busy, slots that become due are picked up here, not left waiting
                # for the next run (which cannot start before this one ends).
                if self.local_today(now) != day:
                    day = self.local_today(now)
                    self.ensure_slots(now)
                fresh = [r for r in self.due_slots(now) if (r.brand, r.slot_date, r.slot) not in known]
                if fresh:
                    if any(self.publishable(r, now) for r in fresh):
                        self.ensure_tools()
                    known.update((r.brand, r.slot_date, r.slot) for r in fresh)
                    pending = deque(sorted([*pending, *fresh], key=lambda r: r.target))
                    log.info("also due now: %s", ", ".join(f"{r.brand}/{r.slot}@{self.fmt(r.target)}" for r in fresh))
            while accepting and pending and pending[0].target <= now:
                chains.extend(self.start_slot(pending.popleft(), now))
                now = self.clock.now()

            # (priority, ready_at, kind, obj): checks first, then deadline chains, then the rest.
            # While a deadline post is in progress, other posts wait so they cannot eat its time.
            items = [(0, k.ready_at, "check", k) for k in checks]
            deadline_active = any(c.queue and c.slot.deadline for c in chains)
            items += [(1 if c.slot.deadline else 2, c.ready_at, "chain", c) for c in chains
                      if c.queue and (c.slot.deadline or not deadline_active)]
            next_times = [i[1] for i in items]
            if accepting and pending:
                next_times.append(pending[0].target)
            if not next_times:
                break
            earliest = min(next_times)
            if earliest > now:
                if accepting and pending and pending[0].target == earliest:
                    self.prefetch(pending[0])
                    if self.clock.now() >= earliest:
                        continue
                self.clock.sleep_until(earliest)
                continue
            ready = [i for i in items if i[1] <= now]
            if not ready:
                continue
            _, _, kind, obj = min(ready, key=lambda i: (i[0], i[1]))
            if kind == "check":
                self.process_check(obj, checks)
                chain = obj.chain
            else:
                self.process_next(obj, checks)
                chain = obj
            if chain.finished and chain in chains:
                chains.remove(chain)
                self.finish_chain(chain)

        if pending:
            log.warning("max runtime reached; %d slot(s) left for the next run", len(pending))
        log.info("run finished: %s", self.report.summary())
        return self.report

    # ------------------------------------------------------------ deliveries

    def context(self, chain: Chain, d: DeliveryRow) -> PublishContext:
        cfg = chain.brand.platform_for(chain.stream, d.platform)
        texts = render(chain.brand.templates[d.platform], d.platform, chain.variables, self.settings.text_limits)

        def fetch():
            if chain.video.local_path is None:
                chain.video.local_path = self.content.fetch(chain.video)
            return chain.video.local_path

        def media_info():
            if chain.media is None:
                chain.media = self.content.probe(chain.video)
            return chain.media

        return PublishContext(brand=chain.brand, stream=chain.stream, platform=d.platform, cfg=cfg,
                              post_id=chain.post_id, video=chain.video, texts=texts,
                              deadline=chain.slot.deadline, services=self.services, fetch_video=fetch,
                              media_info=media_info)

    def process_next(self, chain: Chain, checks: list[Check]) -> None:
        d = self.state.delivery(chain.queue.popleft())
        now = self.clock.now()
        where = f"{chain.brand.name}/{chain.stream} → {d.platform}"
        if chain.slot.deadline is not None and now >= chain.slot.deadline - self.margin():
            log.warning("%s: skipped, deadline %s too close", where, self.fmt(chain.slot.deadline))
            self.state.update_delivery(d.id, now, status="skipped_deadline", error="deadline reached")
            chain.skipped_deadline.append(d.platform)
            self.report.skipped.append(where)
        else:
            try:
                outcome = self.publishers(d.platform, d.provider).publish(self.context(chain, d))
            except Exception as exc:  # noqa: BLE001 - any platform error must not stop the run
                log.exception("%s: publish raised", where)
                outcome = Outcome(status="failed", error=f"{type(exc).__name__}: {exc}")
            self.apply(chain, d, outcome, checks)
        chain.ready_at = self.clock.now() + self.next_gap(chain)

    def next_gap(self, chain: Chain) -> timedelta:
        """Random gap; for deadline posts, shrunk so the remaining platforms still fit before the deadline."""
        gap = self.random_gap(chain.gap)
        if chain.slot.deadline is None or not chain.queue:
            return gap
        budget = (chain.slot.deadline - self.margin() - self.clock.now()).total_seconds()
        per_platform = budget / len(chain.queue) - EST_PUBLISH_SECONDS
        return min(gap, timedelta(seconds=max(MIN_DEADLINE_GAP_SECONDS, round(per_platform))))

    def process_check(self, check: Check, checks: list[Check]) -> None:
        chain = check.chain
        d = self.state.delivery(check.delivery_id)
        now = self.clock.now()
        publisher = self.publishers(d.platform, d.provider)
        ctx = self.context(chain, d)
        checks.remove(check)
        if chain.slot.deadline is not None and now >= chain.slot.deadline:
            try:
                publisher.cancel(ctx, d)
            except Exception:  # noqa: BLE001
                log.exception("cancel failed")
            self.state.update_delivery(d.id, now, status="skipped_deadline", error="not published before deadline")
            chain.skipped_deadline.append(d.platform)
            chain.outstanding_checks -= 1
            return
        try:
            outcome = publisher.check(ctx, d)
        except Exception as exc:  # noqa: BLE001
            log.exception("%s check raised", d.platform)
            outcome = Outcome(status="failed", error=f"{type(exc).__name__}: {exc}")
        chain.outstanding_checks -= 1
        self.apply(chain, d, outcome, checks, counted_attempt=False)

    def apply(self, chain: Chain, d: DeliveryRow, outcome: Outcome, checks: list[Check],
              counted_attempt: bool = True) -> None:
        now = self.clock.now()
        where = f"{chain.brand.name}/{chain.stream} → {d.platform}"
        inc = 1 if counted_attempt else 0
        if outcome.status == "sent":
            self.state.update_delivery(d.id, now, status="sent", attempts_inc=inc, external_id=outcome.external_id,
                                       url=outcome.url, provider=outcome.provider)
            self.report.published.append(where)
            log.info("✓ %s %s", where, outcome.url or outcome.external_id or "")
        elif outcome.status == "manual":
            self.state.update_delivery(d.id, now, status="manual", attempts_inc=inc, error=outcome.error,
                                       provider=outcome.provider)
            self.report.manual.append(where)
            log.info("✋ %s handed over on Telegram", where)
        elif outcome.status == "checking":
            check_after = outcome.check_after or now + timedelta(seconds=30)
            self.state.update_delivery(d.id, now, status="checking", attempts_inc=inc,
                                       external_id=outcome.external_id, provider=outcome.provider,
                                       check_after=check_after)
            chain.outstanding_checks += 1
            checks.append(Check(chain, d.id, check_after))
        else:
            attempts = d.attempts + inc
            give_up = attempts >= self.settings.max_attempts_per_delivery or not outcome.retryable
            self.state.update_delivery(d.id, now, status="gave_up" if give_up else "failed", attempts_inc=inc,
                                       error=outcome.error, provider=outcome.provider)
            self.report.failed.append(where)
            log.error("✗ %s: %s", where, outcome.error)
            tail = "Giving up on this platform." if give_up else (
                f"Will retry on a later run (attempt {attempts}/{self.settings.max_attempts_per_delivery}).")
            self.notifier.alert(f"Publish failed: {where}\nVideo: {chain.video.name}\nError: {outcome.error}\n{tail}")
        self.checkpoint(f"{d.platform} {chain.brand.id}")

    # ------------------------------------------------------------ finishing

    def finish_chain(self, chain: Chain) -> None:
        now = self.clock.now()
        if chain.skipped_deadline:
            self.notifier.alert(
                f"{chain.brand.name} / {chain.slot.slot}: not published before the deadline "
                f"{self.fmt(chain.slot.deadline)} on: {', '.join(chain.skipped_deadline)}")
        self.finish_post(chain.post_id, chain.slot, now)
        try:
            self.content.release(chain.video)
        except Exception:  # noqa: BLE001
            log.exception("could not clean up %s", chain.video.name)
        self.checkpoint(f"finish post {chain.post_id}")

    def finish_post(self, post_id: int, slot: SlotRow, now: datetime) -> None:
        ds = self.state.deliveries(post_id)
        max_attempts = self.settings.max_attempts_per_delivery
        delivered = [d for d in ds if d.status in ("sent", "manual")]
        retrying = [d for d in ds if d.status in ("failed", "pending", "checking") and d.attempts < max_attempts]
        if len(delivered) == len(ds):
            status = "done"
        elif retrying and slot.deadline is None:
            status = "retrying"
        elif delivered:
            status = "partial"
        else:
            status = "failed"
        if status != "retrying":
            # anything still open is final now
            for d in ds:
                if d.status in ("failed", "pending"):
                    self.state.update_delivery(d.id, now, status="gave_up")
        self.state.set_post_status(post_id, status, now)
        posts = self.state.posts_for_slot(slot.brand, slot.slot_date, slot.slot)
        if all(p["status"] in ("done", "partial", "failed") for p in posts):
            slot_status = "done" if all(p["status"] == "done" for p in posts) else "partial"
            self.state.set_slot_status(slot.brand, slot.slot_date, slot.slot, slot_status, now)
