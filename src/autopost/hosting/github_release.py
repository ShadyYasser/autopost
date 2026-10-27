"""Temporary public video URLs as assets of a GitHub Release (tag "media-temp") in this public repo.

Buffer needs a public URL it can fetch. The video is uploaded as a release asset just before
the Buffer post is created and deleted as soon as Buffer reports the post sent or failed.
Stale assets (from crashed runs) are cleaned up at the start of every run.
"""

from __future__ import annotations

import logging
import os
import secrets as pysecrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

log = logging.getLogger(__name__)

API = "https://api.github.com"
UPLOADS = "https://uploads.github.com"


class HostingError(Exception):
    pass


class GitHubReleaseHost:
    def __init__(self, repo: str, token: str, tag: str = "media-temp", session: requests.Session | None = None):
        self.repo = repo
        self.tag = tag
        self.session = session or requests.Session()
        self.headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2022-11-28"}
        self._release: dict | None = None

    @classmethod
    def from_env(cls, tag: str) -> "GitHubReleaseHost | None":
        repo, token = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_TOKEN")
        return cls(repo, token, tag) if repo and token else None

    def _req(self, method: str, url: str, **kw) -> requests.Response:
        try:
            resp = self.session.request(method, url, headers={**self.headers, **kw.pop("headers", {})},
                                        timeout=kw.pop("timeout", 60), **kw)
        except requests.RequestException as exc:
            raise HostingError(f"GitHub {method} failed: {type(exc).__name__}") from exc
        return resp

    def release(self) -> dict:
        if self._release is None:
            resp = self._req("GET", f"{API}/repos/{self.repo}/releases/tags/{self.tag}")
            if resp.status_code == 404:
                resp = self._req("POST", f"{API}/repos/{self.repo}/releases", json={
                    "tag_name": self.tag, "name": "media-temp (automatic, temporary video files)",
                    "body": "Temporary public video files for Buffer. Assets are deleted automatically.",
                    "prerelease": True})
                log.info("created release %s", self.tag)
            if resp.status_code >= 300:
                raise HostingError(f"release {self.tag}: HTTP {resp.status_code} {resp.text[:200]}")
            self._release = resp.json()
        return self._release

    def upload(self, path: Path, label: str) -> tuple[str, int]:
        """Upload a file; returns (public download URL, asset id)."""
        rel = self.release()
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in label)[:60]
        name = f"{safe}-{pysecrets.token_hex(4)}{path.suffix or '.mp4'}"
        with path.open("rb") as fh:
            resp = self._req("POST", f"{UPLOADS}/repos/{self.repo}/releases/{rel['id']}/assets",
                             params={"name": name}, data=fh, timeout=1800,
                             headers={"Content-Type": "video/mp4", "Content-Length": str(path.stat().st_size)})
        if resp.status_code >= 300:
            raise HostingError(f"asset upload: HTTP {resp.status_code} {resp.text[:200]}")
        asset = resp.json()
        log.info("hosted %s (%.1f MB) as %s", path.name, path.stat().st_size / 1_048_576, asset["browser_download_url"])
        return asset["browser_download_url"], int(asset["id"])

    def delete(self, asset_id: int | str) -> None:
        resp = self._req("DELETE", f"{API}/repos/{self.repo}/releases/assets/{asset_id}")
        if resp.status_code not in (204, 404):
            log.warning("could not delete release asset %s: HTTP %s", asset_id, resp.status_code)

    def cleanup(self, older_than: timedelta = timedelta(hours=6)) -> int:
        """Delete leftovers from crashed runs."""
        try:
            rel = self.release()
            resp = self._req("GET", f"{API}/repos/{self.repo}/releases/{rel['id']}/assets", params={"per_page": 100})
            assets = resp.json() if resp.status_code == 200 else []
        except HostingError as exc:
            log.warning("media-temp cleanup skipped: %s", exc)
            return 0
        cutoff = datetime.now(timezone.utc) - older_than
        removed = 0
        for a in assets:
            created = datetime.fromisoformat(a["created_at"].replace("Z", "+00:00"))
            if created < cutoff:
                self.delete(a["id"])
                removed += 1
        if removed:
            log.info("media-temp: removed %d stale asset(s)", removed)
        return removed
