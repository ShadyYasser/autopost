"""Maps (platform, provider) to a publisher instance."""

from __future__ import annotations

from .base import Outcome, Publisher, PublishContext, Services
from .meta import FacebookPublisher, InstagramPublisher
from .pinterest import PinterestPublisher
from .youtube import YoutubeDirectPublisher


class NotYetAvailable:
    """Placeholder for providers that arrive in a later phase."""

    def __init__(self, what: str):
        self.what = what

    def publish(self, ctx: PublishContext) -> Outcome:
        return Outcome(status="failed", error=f"{self.what} is not implemented yet", retryable=False)

    def check(self, ctx: PublishContext, delivery) -> Outcome:
        return self.publish(ctx)

    def cancel(self, ctx: PublishContext, delivery) -> None:
        pass


class PublisherRegistry:
    def __init__(self, services: Services):
        self.services = services
        self._cache: dict[tuple[str, str], Publisher] = {}

    def __call__(self, platform: str, provider: str) -> Publisher:
        key = (platform, provider)
        if key not in self._cache:
            self._cache[key] = self._build(platform, provider)
        return self._cache[key]

    def _build(self, platform: str, provider: str) -> Publisher:
        if provider == "buffer":
            return NotYetAvailable("Buffer publishing (phase 4)")
        if platform == "facebook":
            return FacebookPublisher(self.services)
        if platform == "instagram":
            return InstagramPublisher(self.services)
        if platform == "pinterest":
            return PinterestPublisher(self.services)
        if platform == "youtube":
            return YoutubeDirectPublisher(self.services)
        raise ValueError(f"no direct publisher for {platform}")
