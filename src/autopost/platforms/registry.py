"""Maps (platform, provider) to a publisher instance."""

from __future__ import annotations

from ..hosting.github_release import GitHubReleaseHost
from .base import Publisher, Services
from .buffer import BufferPublisher
from .meta import FacebookPublisher, InstagramPublisher
from .pinterest import PinterestPublisher
from .youtube import YoutubeDirectPublisher


class PublisherRegistry:
    def __init__(self, services: Services, host: GitHubReleaseHost | None = None):
        self.services = services
        self.host = host or GitHubReleaseHost.from_env(services.app.settings.media_hosting.github_release_tag)
        self._cache: dict[tuple[str, str], Publisher] = {}

    def __call__(self, platform: str, provider: str) -> Publisher:
        key = (platform, provider)
        if key not in self._cache:
            self._cache[key] = self._build(platform, provider)
        return self._cache[key]

    def _build(self, platform: str, provider: str) -> Publisher:
        if provider == "buffer":
            return BufferPublisher(self.services, self.host, direct_youtube=self("youtube", "direct"))
        if platform == "facebook":
            return FacebookPublisher(self.services)
        if platform == "instagram":
            return InstagramPublisher(self.services, self.host)
        if platform == "pinterest":
            return PinterestPublisher(self.services)
        if platform == "youtube":
            return YoutubeDirectPublisher(self.services)
        raise ValueError(f"no direct publisher for {platform}")
