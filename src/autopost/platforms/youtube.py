"""Direct YouTube (Kalima Sawaa long videos, and the Shorts fallback when Buffer cannot take a video).

While `youtube_direct.enabled` is false (API audit pending, uploads would be locked private)
this hands the video over on Telegram: Drive link + title + description, and you upload it.
When enabled, it does a Data API v3 resumable upload as PRIVATE and sends you the Studio link.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import requests

from .base import Outcome, PublishContext

log = logging.getLogger(__name__)

TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
CHUNK = 16 * 1024 * 1024


class YoutubeDirectPublisher:
    def __init__(self, services, session: requests.Session | None = None):
        self.services = services
        self.session = session or requests.Session()

    @property
    def enabled(self) -> bool:
        return self.services.app.settings.youtube_direct.enabled

    def publish(self, ctx: PublishContext, reason: str | None = None) -> Outcome:
        if not self.enabled:
            return self.hand_over(ctx, reason)
        try:
            video_id = self.upload(ctx)
        except Exception as exc:  # noqa: BLE001
            log.exception("youtube upload failed")
            return Outcome(status="failed", error=f"youtube upload: {exc}", provider="direct")
        studio = f"https://studio.youtube.com/video/{video_id}/edit"
        self.services.notifier.send(
            f"🎬 {ctx.brand.name}: uploaded to YouTube as PRIVATE. Review and publish it:\n{studio}\n\n"
            f"Title: {ctx.texts['title']}")
        return Outcome(status="sent", external_id=video_id, url=studio, provider="direct")

    def hand_over(self, ctx: PublishContext, reason: str | None = None) -> Outcome:
        link = ctx.video.web_link or f"https://drive.google.com/file/d/{ctx.video.id}/view"
        why = f"\n({reason})" if reason else ""
        ok = self.services.notifier.send(
            f"🎬 {ctx.brand.name} — please upload to YouTube yourself{why}\n\n"
            f"Video: {link}\n\nTitle:\n{ctx.texts['title']}\n\nDescription:\n{ctx.texts['description']}")
        if not ok:
            return Outcome(status="failed", error="could not send the YouTube hand-off on Telegram", provider="direct")
        return Outcome(status="manual", provider="direct")

    # ------------------------------------------------------------ Data API v3

    def access_token(self, ctx: PublishContext) -> str:
        s, yd = self.services.secrets, self.services.app.settings.youtube_direct
        resp = self.session.post(TOKEN_URL, data={
            "client_id": s.get(yd.client_id_secret), "client_secret": s.get(yd.client_secret_secret),
            "refresh_token": s.get(ctx.cfg.refresh_token_secret), "grant_type": "refresh_token"}, timeout=60)
        if resp.status_code != 200:
            raise RuntimeError(f"token refresh failed: HTTP {resp.status_code} {resp.text[:200]}")
        return resp.json()["access_token"]

    def upload(self, ctx: PublishContext) -> str:
        path = Path(ctx.fetch_video())
        size = path.stat().st_size
        token = self.access_token(ctx)
        meta = {
            "snippet": {"title": ctx.texts["title"], "description": ctx.texts["description"],
                        "categoryId": ctx.cfg.category_id},
            "status": {"privacyStatus": ctx.cfg.privacy, "selfDeclaredMadeForKids": ctx.cfg.made_for_kids},
        }
        init = self.session.post(UPLOAD_URL, params={"uploadType": "resumable", "part": "snippet,status"},
                                 headers={"Authorization": f"Bearer {token}",
                                          "Content-Type": "application/json; charset=UTF-8",
                                          "X-Upload-Content-Length": str(size), "X-Upload-Content-Type": "video/mp4"},
                                 data=json.dumps(meta), timeout=60)
        if init.status_code != 200 or "Location" not in init.headers:
            raise RuntimeError(f"upload session failed: HTTP {init.status_code} {init.text[:300]}")
        session_url = init.headers["Location"]
        offset = 0
        with path.open("rb") as fh:
            while True:
                fh.seek(offset)
                chunk = fh.read(CHUNK)
                end = offset + len(chunk) - 1
                resp = self.session.put(session_url, data=chunk, timeout=600, headers={
                    "Authorization": f"Bearer {token}", "Content-Range": f"bytes {offset}-{end}/{size}"})
                if resp.status_code in (200, 201):
                    return resp.json()["id"]
                if resp.status_code != 308:
                    raise RuntimeError(f"upload failed: HTTP {resp.status_code} {resp.text[:300]}")
                rng = resp.headers.get("Range")
                offset = int(rng.split("-")[1]) + 1 if rng else 0

    def check(self, ctx: PublishContext, delivery) -> Outcome:
        return Outcome(status="sent", external_id=delivery.external_id)

    def cancel(self, ctx: PublishContext, delivery) -> None:
        pass
