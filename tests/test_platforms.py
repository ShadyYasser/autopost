import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from autopost.alerts.telegram import Notifier
from autopost.content.base import VideoItem
from autopost.media import MediaInfo
from autopost.platforms.base import PublishContext, Services
from autopost.platforms.meta import FacebookPublisher, GraphClient, InstagramPublisher
from autopost.platforms.pinterest import PinterestPublisher
from autopost.platforms.youtube import YoutubeDirectPublisher
from autopost.secret_store import Secrets
from autopost.state import DeliveryRow

NOW = datetime(2026, 10, 27, 12, tzinfo=timezone.utc)
TOKEN = "PAGE-TOKEN-SECRET"


class Resp:
    def __init__(self, status=200, payload=None, headers=None):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}
        self.text = str(self._payload)
        self.content = b"x"

    def json(self):
        return self._payload


class Session:
    """Scripted HTTP session: routes are (method, url substring, response)."""

    def __init__(self, routes):
        self.routes = list(routes)
        self.calls = []

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        for i, (m, frag, resp) in enumerate(self.routes):
            if m == method and frag in url:
                self.routes.pop(i)
                return resp
        raise AssertionError(f"unexpected {method} {url}")

    def post(self, url, **kw):
        return self.request("POST", url, **kw)

    def get(self, url, **kw):
        return self.request("GET", url, **kw)

    def put(self, url, **kw):
        return self.request("PUT", url, **kw)


@pytest.fixture
def video_file(tmp_path):
    p = tmp_path / "v.mp4"
    p.write_bytes(b"0123456789" * 100)
    return p


def make_ctx(app, brand, stream, platform, video_file, texts, info=None, secrets=None, notifier=None):
    services = Services(app=app, state=None, notifier=notifier or Notifier(None, None, dry_run=True),
                        secrets=Secrets(secrets or {"META_PAGE_TOKEN_TAKWIN": TOKEN, "META_PAGE_TOKEN_KALIMA": TOKEN}),
                        content=None, now=lambda: NOW, advance=lambda s: None, dry_run=False)
    b = app.brands[brand]
    ctx = PublishContext(brand=b, stream=stream, platform=platform, cfg=b.platform_for(stream, platform), post_id=1,
                         video=VideoItem(id="drive123", name="68.mp4", created=None,
                                         web_link="https://drive.google.com/file/d/drive123/view"),
                         texts=texts, deadline=None, services=services,
                         fetch_video=lambda: video_file,
                         media_info=lambda: info or MediaInfo(720, 1280, 30, vcodec="h264"))
    return services, ctx


def delivery(external_id, did=1):
    return DeliveryRow(did, 1, "x", "direct", 0, "checking", 1, external_id, None, None, None)


def no_token_in_urls(session):
    assert all(TOKEN not in url and TOKEN not in str(kw.get("params")) for _, url, kw in session.calls)


def test_facebook_reel_flow(app, video_file):
    services, ctx = make_ctx(app, "takwin", "main", "facebook", video_file, {"text": "دعاء"})
    s = Session([
        ("POST", "771477079389943/video_reels", Resp(200, {"video_id": "v1", "upload_url": "https://rupload.facebook.com/video-upload/v26.0/v1"})),
        ("POST", "rupload.facebook.com/video-upload", Resp(200, {"success": True})),
        ("POST", "771477079389943/video_reels", Resp(200, {"success": True})),
        ("GET", "/v1", Resp(200, {"status": {"video_status": "processing", "publishing_phase": {"status": "in_progress"}}})),
        ("GET", "/v1", Resp(200, {"status": {"video_status": "ready", "publishing_phase": {"status": "complete"}},
                                  "permalink_url": "/reel/987"})),
    ])
    pub = FacebookPublisher(services)
    pub._clients[ctx.cfg.token_secret] = GraphClient(TOKEN, "v26.0", s)
    out = pub.publish(ctx)
    assert out.status == "checking" and out.external_id == "v1"
    start, upload, finish = s.calls[:3]
    assert start[2]["data"] == {"upload_phase": "start"}
    assert upload[2]["headers"]["offset"] == "0" and upload[2]["headers"]["file_size"] == "1000"
    assert upload[2]["headers"]["Authorization"] == f"OAuth {TOKEN}"
    assert finish[2]["data"]["video_state"] == "PUBLISHED" and finish[2]["data"]["description"] == "دعاء"
    assert pub.check(ctx, delivery("v1")).status == "checking"
    done = pub.check(ctx, delivery("v1"))
    assert done.status == "sent" and done.url == "https://www.facebook.com/reel/987"
    assert "v26.0" in start[1]
    no_token_in_urls(s)


def test_facebook_long_video_chunked_upload(app, video_file):
    services, ctx = make_ctx(app, "kalima_sawaa", "long", "facebook", video_file, {"text": "حوار"},
                             info=MediaInfo(1280, 720, 900))
    s = Session([
        ("POST", "graph-video.facebook.com/v26.0/1177142762151237/videos",
         Resp(200, {"upload_session_id": "S", "video_id": "V", "start_offset": "0", "end_offset": "600"})),
        ("POST", "/videos", Resp(200, {"start_offset": "600", "end_offset": "1000"})),
        ("POST", "/videos", Resp(200, {"start_offset": "1000", "end_offset": "1000"})),
        ("POST", "/videos", Resp(200, {"success": True})),
    ])
    pub = FacebookPublisher(services)
    pub._clients[ctx.cfg.token_secret] = GraphClient(TOKEN, "v26.0", s)
    out = pub.publish(ctx)
    assert out.status == "checking" and out.external_id == "V"
    chunks = [c for c in s.calls if c[2].get("files")]
    assert [len(c[2]["files"]["video_file_chunk"][1]) for c in chunks] == [600, 400]
    assert s.calls[-1][2]["data"]["upload_phase"] == "finish"


def test_long_reel_becomes_regular_video(app, video_file):
    services, ctx = make_ctx(app, "takwin", "main", "facebook", video_file, {"text": "t"},
                             info=MediaInfo(720, 1280, 120))
    s = Session([("POST", "/videos", Resp(200, {"upload_session_id": "S", "video_id": "V",
                                                "start_offset": "0", "end_offset": "0"})),
                 ("POST", "/videos", Resp(200, {"success": True}))])
    pub = FacebookPublisher(services)
    pub._clients[ctx.cfg.token_secret] = GraphClient(TOKEN, "v26.0", s)
    assert pub.publish(ctx).status == "checking"
    assert not any("video_reels" in url for _, url, _ in s.calls)


def test_graph_error_is_reported_and_auth_errors_not_retried(app, video_file):
    services, ctx = make_ctx(app, "takwin", "main", "facebook", video_file, {"text": "t"})
    s = Session([("POST", "video_reels", Resp(400, {"error": {"message": "Error validating access token",
                                                              "code": 190}}))])
    pub = FacebookPublisher(services)
    pub._clients[ctx.cfg.token_secret] = GraphClient(TOKEN, "v26.0", s)
    out = pub.publish(ctx)
    assert out.status == "failed" and "access token" in out.error and "190" in out.error
    assert out.retryable is False


def test_instagram_resumable_flow(app, video_file):
    services, ctx = make_ctx(app, "takwin", "main", "instagram", video_file, {"text": "كابشن"})
    s = Session([
        ("POST", "17841455147710531/media", Resp(200, {"id": "C1", "uri": "https://rupload.facebook.com/ig-api-upload/v26.0/C1"})),
        ("POST", "rupload.facebook.com/ig-api-upload/v26.0/C1", Resp(200, {"success": True})),
        ("GET", "/C1", Resp(200, {"status_code": "IN_PROGRESS"})),
        ("GET", "/C1", Resp(200, {"status_code": "FINISHED"})),
        ("POST", "17841455147710531/media_publish", Resp(200, {"id": "M1"})),
        ("GET", "/M1", Resp(200, {"permalink": "https://www.instagram.com/reel/abc/"})),
    ])
    pub = InstagramPublisher(services)
    pub._clients[ctx.cfg.token_secret] = GraphClient(TOKEN, "v26.0", s)
    out = pub.publish(ctx)
    assert out.status == "checking" and out.external_id == "C1"
    create = s.calls[0][2]["data"]
    assert create["media_type"] == "REELS" and create["upload_type"] == "resumable" and create["caption"] == "كابشن"
    assert pub.check(ctx, delivery("C1")).status == "checking"
    done = pub.check(ctx, delivery("C1"))
    assert done.status == "sent" and done.external_id == "M1" and done.url.endswith("/reel/abc/")
    no_token_in_urls(s)


def test_instagram_container_error(app, video_file):
    services, ctx = make_ctx(app, "takwin", "main", "instagram", video_file, {"text": "x"})
    s = Session([("GET", "/C9", Resp(200, {"status_code": "ERROR", "status": "Error: unsupported video"}))])
    pub = InstagramPublisher(services)
    pub._clients[ctx.cfg.token_secret] = GraphClient(TOKEN, "v26.0", s)
    out = pub.check(ctx, delivery("C9"))
    assert out.status == "failed" and "unsupported video" in out.error


def test_pinterest_flow_sandbox(app, video_file):
    brand = app.brands["takwin"]
    brand.platforms["pinterest"].board_id = "B1"
    brand.platforms["pinterest"].access_token_secret = "PINTEREST_SANDBOX_TOKEN"
    services, ctx = make_ctx(app, "takwin", "main", "pinterest", video_file,
                             {"title": "عنوان", "description": "وصف"}, secrets={"PINTEREST_SANDBOX_TOKEN": "tok"})
    s = Session([
        ("POST", "api-sandbox.pinterest.com/v5/media", Resp(201, {"media_id": "MD", "upload_url": "https://s3/upload",
                                                                   "upload_parameters": {"key": "k"}})),
        ("POST", "https://s3/upload", Resp(204)),
        ("GET", "/media/MD", Resp(200, {"status": "processing"})),
        ("GET", "/media/MD", Resp(200, {"status": "succeeded"})),
        ("POST", "/pins", Resp(201, {"id": "P1"})),
    ])
    pub = PinterestPublisher(services, session=s)
    assert pub.publish(ctx).status == "checking"
    assert s.calls[1][2]["data"] == {"key": "k"}
    assert pub.check(ctx, delivery("MD")).status == "checking"
    done = pub.check(ctx, delivery("MD"))
    assert done.status == "sent" and done.url == "https://www.pinterest.com/pin/P1/"
    pin = s.calls[-1][2]["json"]
    assert pin["board_id"] == "B1" and pin["media_source"] == {"source_type": "video_id", "media_id": "MD",
                                                                "cover_image_key_frame_time": 1}
    assert s.calls[0][2]["headers"]["Authorization"] == "Bearer tok"


def test_youtube_manual_handoff_while_direct_disabled(app, video_file):
    notifier = Notifier(None, None, dry_run=True)
    services, ctx = make_ctx(app, "kalima_sawaa", "long", "youtube", video_file,
                             {"title": "حوار الثالوث", "description": "حوار الثالوث"}, notifier=notifier)
    out = YoutubeDirectPublisher(services).publish(ctx)
    assert out.status == "manual"
    msg = notifier.sent[-1]
    assert "https://drive.google.com/file/d/drive123/view" in msg and "حوار الثالوث" in msg


def test_youtube_direct_upload_when_enabled(app, video_file):
    app.settings.youtube_direct.enabled = True
    notifier = Notifier(None, None, dry_run=True)
    services, ctx = make_ctx(app, "kalima_sawaa", "long", "youtube", video_file, {"title": "T", "description": "D"},
                             notifier=notifier, secrets={"YOUTUBE_CLIENT_ID": "id", "YOUTUBE_CLIENT_SECRET": "sec",
                                                         "YOUTUBE_REFRESH_TOKEN_KALIMA": "rt"})
    s = Session([("POST", "oauth2.googleapis.com/token", Resp(200, {"access_token": "AT"})),
                 ("POST", "upload/youtube/v3/videos", Resp(200, {}, headers={"Location": "https://up/session"})),
                 ("PUT", "https://up/session", Resp(200, {"id": "YT1"}))])
    out = YoutubeDirectPublisher(services, session=s).publish(ctx)
    assert out.status == "sent" and out.url == "https://studio.youtube.com/video/YT1/edit"
    meta = json.loads(s.calls[1][2]["data"])
    assert meta["status"]["privacyStatus"] == "private"
    assert s.calls[2][2]["headers"]["Content-Range"] == "bytes 0-999/1000"
    assert "studio.youtube.com/video/YT1" in notifier.sent[-1]


def test_instagram_rupload_error_but_container_processing_keeps_polling(app, video_file):
    services, ctx = make_ctx(app, "kalima_sawaa", "short", "instagram", video_file, {"text": "x"})
    s = Session([
        ("POST", "17841411727741746/media", Resp(200, {"id": "C2"})),
        ("POST", "rupload.facebook.com", Resp(400, {"debug_info": {"retriable": False, "type": "ProcessingFailedError",
                                                                   "message": "Request processing failed"}})),
        ("GET", "/C2", Resp(200, {"status_code": "IN_PROGRESS", "status": "In Progress: Media is still being processed."})),
        ("GET", "/C2", Resp(200, {"status_code": "FINISHED"})),
        ("POST", "17841411727741746/media_publish", Resp(200, {"id": "M2"})),
        ("GET", "/M2", Resp(200, {"permalink": "https://www.instagram.com/reel/xyz/"})),
        ("POST", "/M2", Resp(200, {"success": True})),           # comments off (Kalima)
    ])
    pub = InstagramPublisher(services)
    pub._clients[ctx.cfg.token_secret] = GraphClient(TOKEN, "v26.0", s)
    out = pub.publish(ctx)
    assert out.status == "checking" and out.external_id == "C2"
    assert pub.check(ctx, delivery("C2")).url == "https://www.instagram.com/reel/xyz/"


def test_instagram_turns_comments_off_for_kalima(app, video_file):
    notifier = Notifier(None, None, dry_run=True)
    services, ctx = make_ctx(app, "kalima_sawaa", "short", "instagram", video_file, {"text": "x"}, notifier=notifier)
    s = Session([
        ("GET", "/C3", Resp(200, {"status_code": "FINISHED"})),
        ("POST", "17841411727741746/media_publish", Resp(200, {"id": "M3"})),
        ("GET", "/M3", Resp(200, {"permalink": "https://www.instagram.com/reel/k/"})),
        ("POST", "/M3", Resp(200, {"success": True})),
    ])
    pub = InstagramPublisher(services)
    pub._clients[ctx.cfg.token_secret] = GraphClient(TOKEN, "v26.0", s)
    assert pub.check(ctx, delivery("C3")).status == "sent"
    assert s.calls[-1][2]["data"] == {"comment_enabled": "false"}
    assert notifier.sent == []


def test_instagram_comment_permission_missing_alerts_but_keeps_post(app, video_file):
    notifier = Notifier(None, None, dry_run=True)
    services, ctx = make_ctx(app, "kalima_sawaa", "short", "instagram", video_file, {"text": "x"}, notifier=notifier)
    s = Session([
        ("GET", "/C4", Resp(200, {"status_code": "FINISHED"})),
        ("POST", "media_publish", Resp(200, {"id": "M4"})),
        ("GET", "/M4", Resp(200, {"permalink": "https://www.instagram.com/reel/m/"})),
        ("POST", "/M4", Resp(403, {"error": {"message": "(#10) Application does not have permission", "code": 10}})),
    ])
    pub = InstagramPublisher(services)
    pub._clients[ctx.cfg.token_secret] = GraphClient(TOKEN, "v26.0", s)
    out = pub.check(ctx, delivery("C4"))
    assert out.status == "sent"
    assert "instagram_manage_comments" in notifier.sent[-1]


def test_takwin_comments_stay_on(app, video_file):
    services, ctx = make_ctx(app, "takwin", "main", "instagram", video_file, {"text": "x"})
    s = Session([("GET", "/C5", Resp(200, {"status_code": "FINISHED"})),
                 ("POST", "media_publish", Resp(200, {"id": "M5"})),
                 ("GET", "/M5", Resp(200, {"permalink": "p"}))])
    pub = InstagramPublisher(services)
    pub._clients[ctx.cfg.token_secret] = GraphClient(TOKEN, "v26.0", s)
    assert pub.check(ctx, delivery("C5")).status == "sent" and len(s.calls) == 3
