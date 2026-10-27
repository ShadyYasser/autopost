"""Facebook Page + Instagram publishing via the Meta Graph API with direct file upload.

Tokens go in the Authorization header (never in URLs), so they cannot leak into logs.

Facebook short videos → Reels (video_reels: start → upload to rupload.facebook.com → finish).
Facebook long videos  → regular Page videos (chunked upload on graph-video.facebook.com).
Reels longer than 90 s cannot be Reels, so they are posted as regular videos.
Instagram → REELS container with upload_type=resumable, binary upload to rupload.facebook.com,
            poll the container, then media_publish.
Processing is polled asynchronously (Outcome "checking"), so other platforms are not blocked.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from .base import Outcome, PublishContext

log = logging.getLogger(__name__)

GRAPH = "https://graph.facebook.com/{version}"
GRAPH_VIDEO = "https://graph-video.facebook.com/{version}"
RUPLOAD_IG = "https://rupload.facebook.com/ig-api-upload/{version}/{container}"
REEL_MAX_SECONDS = 90
TRANSIENT_CODES = {1, 2, 4, 17, 32, 341, 368, 613, 9007}
POLL_SECONDS = 15
MAX_IN_MEMORY = 512 * 1024 * 1024
POLL_TIMEOUT = timedelta(minutes=25)


class GraphError(Exception):
    def __init__(self, message: str, code: int | None = None, subcode: int | None = None, transient: bool = False):
        super().__init__(message)
        self.code = code
        self.subcode = subcode
        self.transient = transient

    def __str__(self) -> str:
        extra = f" (code {self.code}{'/' + str(self.subcode) if self.subcode else ''})" if self.code else ""
        return super().__str__() + extra


class GraphClient:
    def __init__(self, token: str, version: str, session: requests.Session | None = None, attempts: int = 3):
        self.token = token
        self.version = version
        self.session = session or requests.Session()
        self.attempts = attempts

    @property
    def headers(self) -> dict:
        return {"Authorization": f"OAuth {self.token}"}

    def call(self, method: str, path: str, *, data: dict | None = None, params: dict | None = None,
             files: dict | None = None, video_host: bool = False, timeout: int = 120) -> dict:
        base = (GRAPH_VIDEO if video_host else GRAPH).format(version=self.version)
        url = f"{base}/{path.lstrip('/')}"
        for attempt in range(1, self.attempts + 1):
            try:
                resp = self.session.request(method, url, data=data, params=params, files=files,
                                            headers=self.headers, timeout=timeout)
            except requests.RequestException as exc:
                if attempt == self.attempts:
                    raise GraphError(f"network error: {type(exc).__name__}", transient=True) from exc
                time.sleep(3 * attempt)
                continue
            try:
                body = resp.json()
            except ValueError:
                body = {}
            if resp.status_code < 400 and "error" not in body:
                return body
            err = _graph_error(resp.status_code, body)
            if err.transient and attempt < self.attempts and files is None:
                time.sleep(3 * attempt)
                continue
            raise err
        raise GraphError("unreachable")

    def rupload_status(self, url: str) -> dict:
        """What the upload server holds for this session (used to resume); {} if it does not say."""
        try:
            resp = self.session.get(url, headers=self.headers, timeout=60)
            return resp.json() if resp.content else {}
        except (requests.RequestException, ValueError):
            return {}

    def rupload(self, url: str, path: Path, timeout: int = 1800, offset: int = 0) -> dict:
        size = path.stat().st_size
        headers = {**self.headers, "offset": str(offset), "file_size": str(size),
                   "Content-Type": "application/octet-stream"}
        # Send the (rest of the) file as one body with an exact Content-Length.
        if size - offset <= MAX_IN_MEMORY:
            with path.open("rb") as fh:
                fh.seek(offset)
                body = fh.read()
        else:
            body = None
        for attempt in range(1, self.attempts + 1):
            try:
                if body is not None:
                    resp = self.session.post(url, data=body, headers=headers, timeout=timeout)
                else:
                    with path.open("rb") as fh:
                        resp = self.session.post(url, data=fh, headers=headers, timeout=timeout)
            except requests.RequestException as exc:
                if attempt == self.attempts:
                    raise GraphError(f"upload network error: {type(exc).__name__}", transient=True) from exc
                time.sleep(5 * attempt)
                continue
            try:
                body = resp.json()
            except ValueError:
                body = {}
            if resp.status_code < 400 and "error" not in body and body.get("success", True) is not False:
                return body
            err = _graph_error(resp.status_code, body if "error" in body else {"error": {"message": resp.text[:300]}})
            if resp.status_code >= 500 and attempt < self.attempts:
                time.sleep(5 * attempt)
                continue
            raise err
        raise GraphError("unreachable")


def _graph_error(status: int, body: dict) -> GraphError:
    e = body.get("error") or {}
    if isinstance(e, str):
        e = {"message": e}
    code = e.get("code")
    msg = e.get("error_user_msg") or e.get("message") or f"HTTP {status}"
    transient = bool(e.get("is_transient")) or code in TRANSIENT_CODES or status >= 500
    return GraphError(msg, code, e.get("error_subcode"), transient)


class _MetaBase:
    def __init__(self, services):
        self.services = services
        self._clients: dict[str, GraphClient] = {}
        self._poll_started: dict[int, float] = {}

    def client(self, ctx: PublishContext) -> GraphClient:
        name = ctx.cfg.token_secret
        if name not in self._clients:
            self._clients[name] = GraphClient(self.services.secrets.get(name), self.services.app.settings.meta.graph_version)
        return self._clients[name]

    def later(self, seconds: int = POLL_SECONDS):
        return self.services.now() + timedelta(seconds=seconds)

    def timed_out(self, delivery_id: int) -> bool:
        started = self._poll_started.setdefault(delivery_id, time.monotonic())
        return time.monotonic() - started > POLL_TIMEOUT.total_seconds()

    @staticmethod
    def failed(exc: Exception) -> Outcome:
        # Expired/invalid tokens and missing permissions will not fix themselves on retry.
        code = getattr(exc, "code", None)
        retryable = not (code in (10, 102, 190) or (isinstance(code, int) and 200 <= code < 300))
        return Outcome(status="failed", error=str(exc), retryable=retryable)


class FacebookPublisher(_MetaBase):
    def publish(self, ctx: PublishContext) -> Outcome:
        path = Path(ctx.fetch_video())
        info = ctx.media_info()
        g = self.client(ctx)
        page = ctx.cfg.page_id
        text = ctx.texts["text"]
        as_reel = ctx.cfg.format == "reel"
        if as_reel and info is not None and info.duration_s > REEL_MAX_SECONDS:
            log.info("facebook: %.0fs is longer than a Reel allows; posting as a regular video", info.duration_s)
            as_reel = False
        try:
            if as_reel:
                start = g.call("POST", f"{page}/video_reels", data={"upload_phase": "start"})
                video_id = start["video_id"]
                g.rupload(start["upload_url"], path)
                g.call("POST", f"{page}/video_reels", data={
                    "upload_phase": "finish", "video_id": video_id, "video_state": "PUBLISHED", "description": text})
            else:
                video_id = self._chunked_upload(g, page, path, text)
        except (GraphError, KeyError) as exc:
            return self.failed(exc)
        log.info("facebook: uploaded video %s (%s); waiting for processing", video_id, "reel" if as_reel else "video")
        return Outcome(status="checking", external_id=str(video_id), check_after=self.later())

    def _chunked_upload(self, g: GraphClient, page: str, path: Path, text: str) -> str:
        size = path.stat().st_size
        start = g.call("POST", f"{page}/videos", data={"upload_phase": "start", "file_size": str(size)},
                       video_host=True)
        session_id, video_id = start["upload_session_id"], start["video_id"]
        offset, end = int(start["start_offset"]), int(start["end_offset"])
        with path.open("rb") as fh:
            while offset < end:
                fh.seek(offset)
                chunk = fh.read(end - offset)
                resp = g.call("POST", f"{page}/videos", video_host=True, timeout=600,
                              data={"upload_phase": "transfer", "upload_session_id": session_id,
                                    "start_offset": str(offset)},
                              files={"video_file_chunk": ("chunk", chunk, "application/octet-stream")})
                offset, end = int(resp["start_offset"]), int(resp["end_offset"])
        g.call("POST", f"{page}/videos", video_host=True, data={
            "upload_phase": "finish", "upload_session_id": session_id, "description": text, "published": "true"})
        return str(video_id)

    def check(self, ctx: PublishContext, delivery) -> Outcome:
        g = self.client(ctx)
        try:
            data = g.call("GET", delivery.external_id, params={"fields": "status,permalink_url"})
        except GraphError as exc:
            return self.failed(exc) if not exc.transient else Outcome(
                status="checking", external_id=delivery.external_id, check_after=self.later(30))
        status = data.get("status") or {}
        errors = [e.get("message") for phase in ("uploading_phase", "processing_phase", "publishing_phase")
                  for e in ((status.get(phase) or {}).get("errors") or [])]
        if status.get("video_status") in ("error", "expired") or errors:
            return Outcome(status="failed", external_id=delivery.external_id,
                           error="facebook processing failed: " + ("; ".join(filter(None, errors)) or status.get("video_status", "")))
        publishing = (status.get("publishing_phase") or {}).get("status")
        done = publishing == "complete" or (publishing is None and status.get("video_status") == "ready")
        if done:
            link = data.get("permalink_url") or f"/{delivery.external_id}"
            return Outcome(status="sent", external_id=delivery.external_id,
                           url=link if link.startswith("http") else f"https://www.facebook.com{link}")
        if self.timed_out(delivery.id):
            return Outcome(status="failed", external_id=delivery.external_id,
                           error=f"facebook still processing after {POLL_TIMEOUT} ({status})")
        return Outcome(status="checking", external_id=delivery.external_id, check_after=self.later())

    def cancel(self, ctx: PublishContext, delivery) -> None:
        """Deadline passed before Facebook finished: delete it so it never appears late."""
        try:
            self.client(ctx).call("DELETE", delivery.external_id)
            log.info("facebook: deleted late video %s", delivery.external_id)
        except GraphError as exc:
            log.warning("facebook: could not delete late video %s: %s", delivery.external_id, exc)


class InstagramPublisher(_MetaBase):
    def publish(self, ctx: PublishContext) -> Outcome:
        path = Path(ctx.fetch_video())
        g = self.client(ctx)
        ig = ctx.cfg.ig_user_id
        try:
            container = g.call("POST", f"{ig}/media", data={
                "media_type": "REELS", "upload_type": "resumable", "caption": ctx.texts["text"],
                "share_to_feed": "true"})
            cid = container["id"]
            uri = container.get("uri") or RUPLOAD_IG.format(version=g.version, container=cid)
        except (GraphError, KeyError) as exc:
            return self.failed(exc)
        try:
            self._upload_with_resume(g, uri, path)
        except GraphError as exc:
            # rupload can answer "ProcessingFailedError" while Instagram is in fact still processing the
            # file it received (seen with longer videos). The container status is the source of truth.
            st = {}
            try:
                st = g.call("GET", cid, params={"fields": "status_code,status"})
            except GraphError:
                pass
            code = st.get("status_code")
            if code in ("IN_PROGRESS", "FINISHED"):
                log.warning("instagram: upload answered %s but container %s is %s; continuing to poll",
                            exc, cid, code)
                return Outcome(status="checking", external_id=str(cid), check_after=self.later())
            detail = f" | container {code}: {st.get('status')}" if st else ""
            log.error("instagram upload failed for %s (%.1f MB)%s", path.name, path.stat().st_size / 1_048_576, detail)
            return Outcome(status="failed", error=f"instagram upload: {exc}{detail}", retryable=True)
        log.info("instagram: uploaded to container %s; waiting for processing", cid)
        return Outcome(status="checking", external_id=str(cid), check_after=self.later(10))

    def _upload_with_resume(self, g: GraphClient, uri: str, path: Path, max_resumes: int = 3) -> None:
        """Upload; if the server reports it holds fewer bytes than the file, resume from its offset."""
        size = path.stat().st_size
        offset = 0
        for attempt in range(max_resumes + 1):
            try:
                g.rupload(uri, path, offset=offset)
                return
            except GraphError as exc:
                status = g.rupload_status(uri)
                log.warning("instagram: rupload error (%s); server reports %s for %d bytes", exc, status or "nothing", size)
                got = status.get("offset") if isinstance(status, dict) else None
                if isinstance(got, (int, str)) and str(got).isdigit() and int(got) < size and attempt < max_resumes:
                    offset = int(got)
                    log.info("instagram: resuming upload at byte %d of %d", offset, size)
                    continue
                raise

    def check(self, ctx: PublishContext, delivery) -> Outcome:
        g = self.client(ctx)
        cid = delivery.external_id
        try:
            data = g.call("GET", cid, params={"fields": "status_code,status"})
        except GraphError as exc:
            return self.failed(exc) if not exc.transient else Outcome(
                status="checking", external_id=cid, check_after=self.later(30))
        code = data.get("status_code")
        if code in ("ERROR", "EXPIRED"):
            return Outcome(status="failed", external_id=cid, error=f"instagram container {code}: {data.get('status')}")
        if code == "FINISHED":
            try:
                media_id = g.call("POST", f"{ctx.cfg.ig_user_id}/media_publish", data={"creation_id": cid})["id"]
            except GraphError as exc:
                if exc.transient and not self.timed_out(delivery.id):
                    return Outcome(status="checking", external_id=cid, check_after=self.later())
                return self.failed(exc)
            url = None
            try:
                url = g.call("GET", media_id, params={"fields": "permalink"}).get("permalink")
            except GraphError:
                pass
            if ctx.brand.comments == "off":
                self.disable_comments(ctx, g, str(media_id), url)
            return Outcome(status="sent", external_id=str(media_id), url=url)
        if code == "PUBLISHED":
            return Outcome(status="sent", external_id=cid)
        if self.timed_out(delivery.id):
            return Outcome(status="failed", external_id=cid, error=f"instagram still processing after {POLL_TIMEOUT}")
        return Outcome(status="checking", external_id=cid, check_after=self.later())

    def disable_comments(self, ctx: PublishContext, g: GraphClient, media_id: str, url: str | None) -> bool:
        """Turn comments off on a published IG media. A failure is reported but does not undo the post."""
        try:
            g.call("POST", media_id, data={"comment_enabled": "false"})
            log.info("instagram: comments turned off on %s", media_id)
            return True
        except GraphError as exc:
            hint = (" The Meta app/token probably lacks the instagram_manage_comments permission: add it and "
                    "regenerate the Page token." if exc.code in (10, 200, 3) or (exc.code or 0) >= 200 else "")
            self.services.notifier.alert(
                f"{ctx.brand.name}: Instagram post published but comments could NOT be turned off: {exc}.{hint}\n"
                f"{url or media_id}")
            return False

    def cancel(self, ctx: PublishContext, delivery) -> None:
        log.info("instagram: container %s left unpublished (deadline)", delivery.external_id)


REQUIRED_SCOPES = ["pages_show_list", "pages_read_engagement", "pages_manage_posts",
                   "instagram_basic", "instagram_content_publish"]


def check_token_scopes(token: str, version: str, extra: list[str] = ()) -> list[str]:
    """Inspect the token with debug_token (a token may inspect itself); returns lines to print."""
    g = GraphClient(token, version)
    try:
        info = g.call("GET", "debug_token", params={"input_token": token}).get("data") or {}
    except GraphError as exc:
        return [f"? token details unavailable ({exc}); permissions will show on the first real post"]
    scopes = set(info.get("scopes") or [])
    expires = info.get("expires_at")
    when = "never" if not expires else datetime.fromtimestamp(expires, timezone.utc).strftime("%Y-%m-%d")
    lines = [f"{'✓' if info.get('is_valid') else '✗'} token: type {info.get('type')}, "
             f"valid={info.get('is_valid')}, expires {when}"]
    for scope in [*REQUIRED_SCOPES, *extra]:
        lines.append(f"{'✓' if scope in scopes else '✗'} permission {scope}")
    return lines


def check_meta_access(token: str, version: str, page_id: str | None, ig_user_id: str | None) -> list[str]:
    """Read-only token check for `autopost check`; returns lines to print."""
    g = GraphClient(token, version)
    out = []
    if page_id:
        try:
            page = g.call("GET", page_id, params={"fields": "id,name"})
            out.append(f"✓ Facebook page {page.get('name')} ({page.get('id')})")
        except GraphError as exc:
            out.append(f"✗ Facebook page {page_id}: {exc}")
    if ig_user_id:
        try:
            ig = g.call("GET", ig_user_id, params={"fields": "id,username"})
            out.append(f"✓ Instagram @{ig.get('username')} ({ig.get('id')})")
        except GraphError as exc:
            out.append(f"✗ Instagram {ig_user_id}: {exc}")
    return out


HIDE_SCOPES = ["pages_read_user_content", "pages_manage_engagement"]


def token_scopes(token: str, version: str) -> set[str] | None:
    try:
        info = GraphClient(token, version).call("GET", "debug_token", params={"input_token": token}).get("data") or {}
    except GraphError:
        return None
    return set(info.get("scopes") or [])


def hide_facebook_comments(app, state, secrets, notifier, now, force: bool = False) -> dict[str, int]:
    """For brands with `comments: off`: hide visitors' comments on their recent Facebook posts.

    Runs at most every `facebook_comment_hiding.interval_minutes` per brand (unless force=True).
    Without the needed permissions it does nothing and alerts once.
    """
    cfg = app.settings.facebook_comment_hiding
    version = app.settings.meta.graph_version
    hidden: dict[str, int] = {}
    for brand in app.enabled_brands():
        fb = brand.platforms.get("facebook")
        if brand.comments != "off" or fb is None or fb.provider != "direct" or not fb.enabled:
            continue
        key = f"fb-comment-sweep:{brand.id}"
        last = state.kv_get(key)
        if not force and last and now - datetime.fromisoformat(last) < timedelta(minutes=cfg.interval_minutes):
            continue
        if not secrets.has(fb.token_secret):
            continue
        token = secrets.get(fb.token_secret)
        scopes = token_scopes(token, version)
        missing = [s for s in HIDE_SCOPES if scopes is not None and s not in scopes]
        if scopes is None or missing:
            notifier.alert(
                f"{brand.name}: Facebook comments cannot be hidden yet. Add these permissions to the Meta app, "
                f"regenerate the Page token and update {fb.token_secret}: {', '.join(missing or HIDE_SCOPES)}",
                key=f"fb-hide-perms:{brand.id}:{','.join(missing)}", state=state, now=now)
            continue
        g = GraphClient(token, version)
        count = 0
        targets = state.delivered_ids(brand.id, "facebook", now - timedelta(days=cfg.recent_days))
        log.info("%s: sweeping comments on %d Facebook post(s)", brand.id, len(targets))
        for object_id in targets:
            try:
                comments = g.call("GET", f"{object_id}/comments", params={
                    "fields": "id,is_hidden,can_hide,from{id}", "filter": "stream", "limit": "100"}).get("data") or []
            except GraphError as exc:
                log.warning("facebook comments of %s: %s", object_id, exc)
                continue
            for c in comments:
                if c.get("is_hidden") or not c.get("can_hide") or (c.get("from") or {}).get("id") == fb.page_id:
                    continue
                try:
                    g.call("POST", c["id"], data={"is_hidden": "true"})
                    count += 1
                except GraphError as exc:
                    log.warning("could not hide comment %s: %s", c.get("id"), exc)
        state.kv_set(key, now.isoformat())
        hidden[brand.id] = count
        log.info("%s: Facebook comment sweep done, %d comment(s) hidden", brand.id, count)
    return hidden
