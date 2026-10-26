"""Publisher interface shared by direct, Buffer and manual providers."""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Callable, Literal, Protocol

if TYPE_CHECKING:
    from ..alerts.telegram import Notifier
    from ..config import AppConfig, BrandConfig, PlatformConfig
    from ..content.base import ContentSource, VideoItem
    from ..secret_store import Secrets
    from ..state import DeliveryRow, State

log = logging.getLogger(__name__)


@dataclass
class Services:
    app: "AppConfig"
    state: "State"
    notifier: "Notifier"
    secrets: "Secrets"
    content: "ContentSource"
    now: Callable[[], datetime]
    advance: Callable[[float], None]       # lets simulated clocks account for work time
    dry_run: bool


@dataclass
class PublishContext:
    brand: "BrandConfig"
    stream: str
    platform: str
    cfg: "PlatformConfig"
    post_id: int
    video: "VideoItem"
    texts: dict[str, str]                  # rendered fields: text | title | description
    deadline: datetime | None
    services: Services
    fetch_video: Callable[[], "object"] = field(repr=False, default=None)  # downloads once, returns local Path
    media_info: Callable[[], "object"] = field(repr=False, default=None)   # MediaInfo (ffprobe, else Drive)


@dataclass
class Outcome:
    status: Literal["sent", "checking", "manual", "failed"]
    external_id: str | None = None
    url: str | None = None
    error: str | None = None
    check_after: datetime | None = None
    provider: str | None = None            # set when a fallback provider was used
    retryable: bool = True


class Publisher(Protocol):
    def publish(self, ctx: PublishContext) -> Outcome: ...

    def check(self, ctx: PublishContext, delivery: "DeliveryRow") -> Outcome: ...

    def cancel(self, ctx: PublishContext, delivery: "DeliveryRow") -> None: ...


class DryRunPublisher:
    """Logs what would be published. Used for every platform in dry-run mode.

    With download=True it also downloads each video once and reports what ffprobe sees.
    """

    def __init__(self, rng: random.Random | None = None, download: bool = False):
        self.rng = rng or random.Random()
        self.download = download

    def publish(self, ctx: PublishContext) -> Outcome:
        fields = " | ".join(f"{k}({len(v)})={_short(v)}" for k, v in ctx.texts.items())
        log.info("[dry-run] %s/%s → %s via %s: %s | %s", ctx.brand.id, ctx.stream, ctx.platform,
                 ctx.cfg.provider, ctx.video.name, fields)
        if self.download and ctx.fetch_video:
            ctx.fetch_video()
        if ctx.platform == "youtube" and ctx.media_info:
            from ..media import shorts_problem

            info = ctx.media_info()
            check = ctx.cfg.shorts_check
            if info is None:
                log.info("[dry-run]   media: unknown (no Drive metadata; use --download to probe)")
            elif check:
                problem = shorts_problem(info, check.vertical, check.max_seconds)
                log.info("[dry-run]   media %s via %s → %s", info.describe(), info.source,
                         f"NOT a Short ({problem}) → fallback {ctx.cfg.fallback}" if problem else "OK as a Short")
            else:
                log.info("[dry-run]   media %s via %s", info.describe(), info.source)
        ctx.services.advance(self.rng.uniform(5, 30))   # pretend the upload took a while
        return Outcome(status="sent", external_id="dry-run")

    def check(self, ctx: PublishContext, delivery: "DeliveryRow") -> Outcome:
        return Outcome(status="sent", external_id=delivery.external_id)

    def cancel(self, ctx: PublishContext, delivery: "DeliveryRow") -> None:
        pass


def _short(value: str, n: int = 60) -> str:
    value = value.replace("\n", " ")
    return value if len(value) <= n else value[: n - 1] + "…"
