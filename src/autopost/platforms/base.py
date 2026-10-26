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
    fetch_video: Callable[[], "object"] = field(repr=False, default=None)  # returns local Path


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
    """Logs what would be published. Used for every platform in dry-run mode."""

    def __init__(self, rng: random.Random | None = None):
        self.rng = rng or random.Random()

    def publish(self, ctx: PublishContext) -> Outcome:
        fields = " | ".join(f"{k}={_short(v)}" for k, v in ctx.texts.items())
        log.info("[dry-run] %s/%s → %s via %s: %s | %s", ctx.brand.id, ctx.stream, ctx.platform,
                 ctx.cfg.provider, ctx.video.name, fields)
        ctx.services.advance(self.rng.uniform(5, 30))   # pretend the upload took a while
        return Outcome(status="sent", external_id="dry-run")

    def check(self, ctx: PublishContext, delivery: "DeliveryRow") -> Outcome:
        return Outcome(status="sent", external_id=delivery.external_id)

    def cancel(self, ctx: PublishContext, delivery: "DeliveryRow") -> None:
        pass


def _short(value: str, n: int = 60) -> str:
    value = value.replace("\n", " ")
    return value if len(value) <= n else value[: n - 1] + "…"
