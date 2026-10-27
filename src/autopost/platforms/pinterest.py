"""Pinterest API v5 video pins: register media → upload → poll → create pin on the brand's board."""

from __future__ import annotations

import logging
import time
from datetime import timedelta
from pathlib import Path

import requests

from .base import Outcome, PublishContext

log = logging.getLogger(__name__)

API = {"production": "https://api.pinterest.com/v5", "sandbox": "https://api-sandbox.pinterest.com/v5"}
OAUTH = "https://api.pinterest.com/v5/oauth/token"
POLL_TIMEOUT = timedelta(minutes=25)


class PinterestError(Exception):
    def __init__(self, message: str, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


class PinterestPublisher:
    def __init__(self, services, session: requests.Session | None = None):
        self.services = services
        self.session = session or requests.Session()
        self._tokens: dict[str, str] = {}
        self._poll_started: dict[int, float] = {}

    @property
    def base(self) -> str:
        return API[self.services.app.settings.pinterest.environment]

    def token(self, ctx: PublishContext) -> str:
        secrets = self.services.secrets
        if ctx.cfg.access_token_secret:
            return secrets.get(ctx.cfg.access_token_secret)
        key = ctx.cfg.refresh_token_secret
        if key not in self._tokens:
            p = self.services.app.settings.pinterest
            resp = self.session.post(OAUTH, auth=(secrets.get(p.app_id_secret), secrets.get(p.app_secret_secret)),
                                     data={"grant_type": "refresh_token", "refresh_token": secrets.get(key)},
                                     timeout=60)
            if resp.status_code != 200:
                raise PinterestError(f"token refresh failed: HTTP {resp.status_code} {_message(resp)}",
                                     retryable=resp.status_code >= 500)
            self._tokens[key] = resp.json()["access_token"]
        return self._tokens[key]

    def call(self, ctx: PublishContext, method: str, path: str, **kw) -> dict:
        headers = {"Authorization": f"Bearer {self.token(ctx)}"}
        try:
            resp = self.session.request(method, f"{self.base}/{path}", headers=headers, timeout=120, **kw)
        except requests.RequestException as exc:
            raise PinterestError(f"network error: {type(exc).__name__}") from exc
        if resp.status_code >= 400:
            raise PinterestError(f"{method} /{path}: HTTP {resp.status_code} {_message(resp)}",
                                 retryable=resp.status_code in (408, 429) or resp.status_code >= 500)
        return resp.json() if resp.content else {}

    def publish(self, ctx: PublishContext) -> Outcome:
        path = Path(ctx.fetch_video())
        try:
            media = self.call(ctx, "POST", "media", json={"media_type": "video"})
            with path.open("rb") as fh:
                up = self.session.post(media["upload_url"], data=media.get("upload_parameters") or {},
                                       files={"file": (path.name, fh, "video/mp4")}, timeout=1800)
            if up.status_code >= 300:
                raise PinterestError(f"video upload failed: HTTP {up.status_code} {up.text[:200]}")
        except (PinterestError, KeyError, requests.RequestException) as exc:
            return Outcome(status="failed", error=f"pinterest: {exc}", retryable=getattr(exc, "retryable", True))
        log.info("pinterest: uploaded media %s; waiting for processing", media["media_id"])
        return Outcome(status="checking", external_id=str(media["media_id"]),
                       check_after=self.services.now() + timedelta(seconds=15))

    def check(self, ctx: PublishContext, delivery) -> Outcome:
        media_id = delivery.external_id
        try:
            status = self.call(ctx, "GET", f"media/{media_id}").get("status")
            if status == "failed":
                return Outcome(status="failed", external_id=media_id, error="pinterest video processing failed")
            if status != "succeeded":
                started = self._poll_started.setdefault(delivery.id, time.monotonic())
                if time.monotonic() - started > POLL_TIMEOUT.total_seconds():
                    return Outcome(status="failed", external_id=media_id, error=f"pinterest media still {status}")
                return Outcome(status="checking", external_id=media_id,
                               check_after=self.services.now() + timedelta(seconds=15))
            pin = self.call(ctx, "POST", "pins", json={
                "board_id": ctx.cfg.board_id,
                "title": ctx.texts["title"],
                "description": ctx.texts["description"],
                "media_source": {"source_type": "video_id", "media_id": media_id, "cover_image_key_frame_time": 1},
            })
        except PinterestError as exc:
            return Outcome(status="failed", external_id=media_id, error=f"pinterest: {exc}", retryable=exc.retryable)
        return Outcome(status="sent", external_id=str(pin["id"]), url=f"https://www.pinterest.com/pin/{pin['id']}/")

    def cancel(self, ctx: PublishContext, delivery) -> None:
        log.info("pinterest: media %s left without a pin (deadline)", delivery.external_id)


def _message(resp: requests.Response) -> str:
    try:
        return str(resp.json().get("message", resp.text[:200]))
    except ValueError:
        return resp.text[:200]
