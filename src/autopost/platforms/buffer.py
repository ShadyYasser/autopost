"""Publishing through Buffer's GraphQL API (https://api.buffer.com, Bearer API key).

Flow per delivery: (YouTube: Shorts check, else direct fallback) → upload the prepared video
to the GitHub Release "media-temp" → createPost(shareNow) → poll the post until sent or error →
delete the release asset. Any Buffer failure hands the video + caption over on Telegram.

Every API call is counted per Buffer account in state and checked against the account's
15-minute / 24-hour / 30-day limits (minus a safety margin) before it is made.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path

import requests

from ..media import shorts_problem
from .base import Outcome, PublishContext

log = logging.getLogger(__name__)

API = "https://api.buffer.com"
POLL_STEPS = [60, 60, 90, 120, 180, 240, 300]   # seconds between status checks (last one repeats)

CREATE_POST = """
mutation($input: CreatePostInput!) {
  createPost(input: $input) {
    __typename
    ... on PostActionSuccess { post { id status } }
    ... on MutationError { message }
  }
}"""
GET_POST = "query($input: PostInput!) { post(input: $input) { id status sentAt externalLink error { message } } }"
DELETE_POST = """
mutation($input: DeletePostInput!) {
  deletePost(input: $input) { __typename ... on MutationError { message } }
}"""
QUEUED_POSTS = """
query($input: PostsInput!) { posts(input: $input, first: 50) { edges { node { id status } } } }"""


def channel_matches(channel: dict, wanted: str) -> bool:
    """Configured channel name vs Buffer's name/displayName (case and extra spaces ignored)."""
    norm = lambda v: " ".join(str(v or "").split()).casefold()  # noqa: E731
    return norm(wanted) in {norm(channel.get("name")), norm(channel.get("displayName"))}


class BufferError(Exception):
    pass


class BudgetExceeded(BufferError):
    pass


class BufferClient:
    def __init__(self, api_key: str, session: requests.Session | None = None, on_request=None):
        self.api_key = api_key
        self.session = session or requests.Session()
        self.on_request = on_request          # called before every request (budget check + counting)

    def query(self, query: str, variables: dict | None = None) -> dict:
        if self.on_request:
            self.on_request()
        try:
            resp = self.session.post(API, json={"query": query, "variables": variables or {}}, timeout=60,
                                     headers={"Authorization": f"Bearer {self.api_key}",
                                              "Content-Type": "application/json"})
        except requests.RequestException as exc:
            raise BufferError(f"network error: {type(exc).__name__}") from exc
        if resp.status_code == 429:
            raise BudgetExceeded(f"Buffer rate limit hit (retry after {resp.headers.get('Retry-After', '?')}s)")
        try:
            body = resp.json()
        except ValueError:
            raise BufferError(f"HTTP {resp.status_code}: {resp.text[:200]}") from None
        if resp.status_code >= 400 or body.get("errors"):
            msgs = "; ".join(e.get("message", "") for e in body.get("errors") or []) or resp.text[:200]
            raise BufferError(f"HTTP {resp.status_code}: {msgs}")
        return body.get("data") or {}

    def account(self) -> dict:
        return self.query("query { account { email organizations { id name } } }")["account"]

    def channels(self, organization_id: str) -> list[dict]:
        return self.query(
            "query($input: ChannelsInput!) { channels(input: $input) "
            "{ id name displayName service isDisconnected isLocked } }",
            {"input": {"organizationId": organization_id}})["channels"]

    def create_post(self, payload: dict) -> dict:
        res = self.query(CREATE_POST, {"input": payload})["createPost"]
        if res.get("__typename") != "PostActionSuccess":
            raise BufferError(f"{res.get('__typename')}: {res.get('message')}")
        return res["post"]

    def post(self, post_id: str) -> dict:
        return self.query(GET_POST, {"input": {"id": post_id}})["post"]

    def delete_post(self, post_id: str) -> None:
        res = self.query(DELETE_POST, {"input": {"id": post_id}})["deletePost"]
        if res.get("__typename") != "DeletePostSuccess":
            raise BufferError(f"deletePost: {res.get('message')}")

    def queued(self, organization_id: str, channel_id: str) -> int:
        data = self.query(QUEUED_POSTS, {"input": {"organizationId": organization_id, "filter": {
            "channelIds": [channel_id], "status": ["scheduled", "sending"]}}})
        return len((data.get("posts") or {}).get("edges") or [])


class BufferBudget:
    """Counts requests per account in state and refuses one that would cross a limit."""

    def __init__(self, state, account: str, limits, now):
        self.state, self.account, self.limits, self.now = state, account, limits, now

    def usage(self) -> dict[str, int]:
        now = self.now()
        return {
            "15min": self.state.buffer_requests_since(self.account, now - timedelta(minutes=15)),
            "24h": self.state.buffer_requests_since(self.account, now - timedelta(days=1)),
            "30d": self.state.buffer_requests_since(self.account, now - timedelta(days=30)),
        }

    def caps(self) -> dict[str, int]:
        m = self.limits.safety_margin
        return {"15min": max(1, self.limits.per_15min - max(2, m // 2)),
                "24h": max(1, self.limits.per_day - m),
                "30d": max(1, self.limits.per_30days - m * 10)}

    def take(self) -> None:
        if self.state is None:
            return
        usage, caps = self.usage(), self.caps()
        for window, used in usage.items():
            if used >= caps[window]:
                raise BudgetExceeded(f"Buffer account {self.account}: {used} requests in {window} "
                                     f"(self-imposed cap {caps[window]})")
        self.state.record_buffer_request(self.account, self.now())


class BufferPublisher:
    def __init__(self, services, host, direct_youtube=None, session: requests.Session | None = None):
        self.services = services
        self.host = host                       # GitHubReleaseHost (None → cannot publish via Buffer)
        self.direct_youtube = direct_youtube   # YoutubeDirectPublisher for the Shorts fallback
        self.session = session
        self._clients: dict[str, BufferClient] = {}
        self._polls: dict[int, int] = {}
        self._elapsed: dict[int, float] = {}

    # ------------------------------------------------------------ helpers

    def client(self, account: str) -> BufferClient:
        if account not in self._clients:
            acct = self.services.app.settings.buffer_accounts[account]
            budget = BufferBudget(self.services.state, account, acct.limits, self.services.now)
            self._clients[account] = BufferClient(self.services.secrets.get(acct.api_key_secret),
                                                  self.session, on_request=budget.take)
        return self._clients[account]

    def _kv(self, key: str) -> str | None:
        return self.services.state.kv_get(key) if self.services.state is not None else None

    def _kv_set(self, key: str, value: str) -> None:
        if self.services.state is not None:
            self.services.state.kv_set(key, value)

    def resolve(self, account: str, channel_name: str) -> tuple[str, str]:
        """(organization id, channel id), cached in state."""
        key = f"buffer:{account}:channel:{' '.join(channel_name.split()).casefold()}"
        cached = self._kv(key)
        if cached:
            org, cid = cached.split("|", 1)
            return org, cid
        client = self.client(account)
        for org in client.account().get("organizations") or []:
            for ch in client.channels(org["id"]):
                if channel_matches(ch, channel_name):
                    if ch.get("isDisconnected") or ch.get("isLocked"):
                        raise BufferError(f"channel {channel_name!r} is disconnected or locked in Buffer")
                    self._kv_set(key, f"{org['id']}|{ch['id']}")
                    return org["id"], ch["id"]
        raise BufferError(f"channel {channel_name!r} not found in Buffer account {account}")

    def payload(self, ctx: PublishContext, channel_id: str, video_url: str) -> dict:
        texts = ctx.texts
        if ctx.platform == "youtube":
            text, title = texts["description"], texts["title"]
            metadata = {"youtube": {"title": title, "privacy": ctx.cfg.buffer_privacy,
                                    "categoryId": ctx.cfg.category_id, "madeForKids": ctx.cfg.made_for_kids,
                                    "notifySubscribers": True}}
        else:
            text, title, metadata = texts["text"], None, None
        payload = {"channelId": channel_id, "text": text, "mode": "shareNow", "schedulingType": "automatic",
                   "assets": [{"video": {"url": video_url, **({"metadata": {"title": title}} if title else {})}}]}
        if metadata:
            payload["metadata"] = metadata
        return payload

    def caption_for_manual(self, ctx: PublishContext) -> str:
        if ctx.platform == "youtube":
            return f"{ctx.texts['title']}\n\n{ctx.texts['description']}"
        return ctx.texts["text"]

    def manual(self, ctx: PublishContext, reason: str) -> Outcome:
        """Buffer could not do it: send the video and caption on Telegram for a manual post."""
        notifier = self.services.notifier
        notifier.send(f"📲 {ctx.brand.name} → {ctx.platform}: Buffer could not publish ({reason}).\n"
                      f"Please post it manually; the video and caption follow.")
        try:
            path = Path(ctx.fetch_video())
        except Exception:  # noqa: BLE001
            path = Path("/nonexistent")
        ok = notifier.send_video(path, self.caption_for_manual(ctx), fallback_link=ctx.video.web_link)
        if not ok:
            return Outcome(status="failed", error=f"{reason}; Telegram hand-off failed too", provider="buffer")
        return Outcome(status="manual", error=reason, provider="buffer")

    def _cleanup(self, asset_id: str | None) -> None:
        if asset_id and self.host is not None:
            self.host.delete(asset_id)

    # ------------------------------------------------------------ publisher API

    def publish(self, ctx: PublishContext) -> Outcome:
        cfg = ctx.cfg
        if ctx.platform == "youtube" and cfg.shorts_check:
            ctx.fetch_video()
            info = ctx.media_info()
            problem = shorts_problem(info, cfg.shorts_check.vertical, cfg.shorts_check.max_seconds) if info else None
            if problem:
                log.info("youtube: not a Short (%s); using the direct fallback", problem)
                if cfg.fallback == "direct" and self.direct_youtube is not None:
                    return self.direct_youtube.publish(ctx, reason=f"not a Short: {problem}")
                return self.manual(ctx, f"not a Short: {problem}")
        if self.host is None:
            return self.manual(ctx, "no public hosting available (GITHUB_TOKEN/GITHUB_REPOSITORY missing)")
        asset_id = None
        try:
            org_id, channel_id = self.resolve(cfg.buffer_account, cfg.channel)
            limit = self.services.app.settings.buffer_accounts[cfg.buffer_account].limits.max_queued
            if self.client(cfg.buffer_account).queued(org_id, channel_id) >= limit:
                raise BufferError(f"{limit} posts already queued on this channel")
            url, asset_id = self.host.upload(Path(ctx.fetch_video()), f"{ctx.brand.id}-{ctx.platform}")
            post = self.client(cfg.buffer_account).create_post(self.payload(ctx, channel_id, url))
        except Exception as exc:  # noqa: BLE001 - every Buffer/hosting problem ends in the Telegram hand-off
            log.error("buffer %s: %s", ctx.platform, exc)
            self._cleanup(asset_id)
            return self.manual(ctx, str(exc))
        log.info("buffer: created %s post %s (%s)", ctx.platform, post["id"], post.get("status"))
        return Outcome(status="checking", external_id=f"{post['id']}|{asset_id}", provider="buffer",
                       check_after=self.services.now() + timedelta(seconds=POLL_STEPS[0]))

    def check(self, ctx: PublishContext, delivery) -> Outcome:
        post_id, _, asset_id = (delivery.external_id or "").partition("|")
        cfg = ctx.cfg
        poll = self.services.app.settings.buffer_accounts[cfg.buffer_account].poll
        n = self._polls[delivery.id] = self._polls.get(delivery.id, 0) + 1
        waited = sum(POLL_STEPS[min(i, len(POLL_STEPS) - 1)] for i in range(n))
        try:
            p = self.client(cfg.buffer_account).post(post_id)
        except BufferError as exc:
            if waited > poll.timeout_minutes * 60 or isinstance(exc, BudgetExceeded):
                self._cleanup(asset_id)
                return self.manual(ctx, f"could not read post status: {exc}")
            return self._again(delivery, n)
        status = p.get("status")
        if status == "sent":
            self._cleanup(asset_id)
            return Outcome(status="sent", external_id=post_id, url=p.get("externalLink"), provider="buffer")
        if status == "error":
            self._cleanup(asset_id)
            return self.manual(ctx, f"Buffer error: {(p.get('error') or {}).get('message', 'unknown')}")
        if waited > poll.timeout_minutes * 60:
            self.cancel(ctx, delivery)
            return self.manual(ctx, f"not sent after {poll.timeout_minutes} min (status {status})")
        return self._again(delivery, n)

    def _again(self, delivery, n: int) -> Outcome:
        step = POLL_STEPS[min(n, len(POLL_STEPS) - 1)]
        return Outcome(status="checking", external_id=delivery.external_id, provider="buffer",
                       check_after=self.services.now() + timedelta(seconds=step))

    def cancel(self, ctx: PublishContext, delivery) -> None:
        post_id, _, asset_id = (delivery.external_id or "").partition("|")
        try:
            p = self.client(ctx.cfg.buffer_account).post(post_id)
            if p.get("status") not in ("sent", "error"):
                self.client(ctx.cfg.buffer_account).delete_post(post_id)
                log.info("buffer: deleted unsent post %s", post_id)
        except BufferError as exc:
            log.warning("buffer: could not cancel post %s: %s", post_id, exc)
        self._cleanup(asset_id)
