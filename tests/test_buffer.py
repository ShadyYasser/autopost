import json
from datetime import datetime, timedelta, timezone

import pytest

from autopost.alerts.telegram import Notifier
from autopost.config import BufferLimits
from autopost.content.base import VideoItem
from autopost.media import MediaInfo
from autopost.platforms.base import PublishContext, Services
from autopost.platforms.buffer import BudgetExceeded, BufferBudget, BufferPublisher, channel_matches
from autopost.secret_store import Secrets
from autopost.state import DeliveryRow, State

NOW = datetime(2026, 10, 27, 12, tzinfo=timezone.utc)


class Resp:
    def __init__(self, payload, status=200):
        self.status_code = status
        self._payload = payload
        self.text = json.dumps(payload)
        self.headers = {}

    def json(self):
        return self._payload


class GraphQLSession:
    """Answers Buffer GraphQL calls by matching a fragment of the query text."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append(json)
        for i, (frag, payload) in enumerate(self.answers):
            if frag in json["query"]:
                self.answers.pop(i)
                return Resp({"data": payload})
        raise AssertionError(f"unexpected query {json['query'][:80]}")


class FakeHost:
    def __init__(self):
        self.uploaded, self.deleted = [], []

    def upload(self, path, label):
        self.uploaded.append(label)
        return f"https://github.com/o/r/releases/download/media-temp/{label}.mp4", 77

    def delete(self, asset_id):
        self.deleted.append(asset_id)


class FakeYoutubeDirect:
    def __init__(self):
        self.calls = []

    def publish(self, ctx, reason=None):
        from autopost.platforms.base import Outcome

        self.calls.append(reason)
        return Outcome(status="manual", provider="direct")


@pytest.fixture
def setup(app, tmp_path):
    state = State(tmp_path / "s.db")
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x" * 100)
    notifier = Notifier(None, None, dry_run=True)
    services = Services(app=app, state=state, notifier=notifier,
                        secrets=Secrets({"BUFFER_API_KEY": "k1", "BUFFER_YOUTUBE_API_KEY": "k2"}),
                        content=None, now=lambda: NOW, advance=lambda s: None, dry_run=False)

    def ctx(brand, stream, platform, texts, info=MediaInfo(720, 1280, 40)):
        b = app.brands[brand]
        return PublishContext(brand=b, stream=stream, platform=platform, cfg=b.platform_for(stream, platform),
                              post_id=1, video=VideoItem(id="d1", name="v.mp4", created=None, web_link="https://drive/d1"),
                              texts=texts, deadline=None, services=services, fetch_video=lambda: video,
                              media_info=lambda: info)
    return services, state, notifier, ctx


ACCOUNT = {"account": {"email": "e", "organizations": [{"id": "ORG", "name": "o"}]}}
CHANNELS = {"channels": [{"id": "CH_TAK", "name": "_takwen", "displayName": "_takwen", "service": "tiktok"},
                         {"id": "CH_KAL", "name": "kalima_sawaa", "displayName": "kalima_sawaa", "service": "tiktok"}]}
YT_CHANNELS = {"channels": [{"id": "YT_TAK", "name": "TAKWEN تكوين ", "displayName": "TAKWEN تكوين ", "service": "youtube"}]}


def delivery(external_id, did=5):
    return DeliveryRow(did, 1, "tiktok", "buffer", 0, "checking", 1, external_id, None, None, None)


def test_tiktok_flow_create_poll_sent_and_cleanup(setup):
    services, state, notifier, ctx = setup
    host = FakeHost()
    s = GraphQLSession([
        ("account", ACCOUNT), ("channels", CHANNELS), ("posts(", {"posts": {"edges": []}}),
        ("createPost", {"createPost": {"__typename": "PostActionSuccess", "post": {"id": "P1", "status": "sending"}}}),
        ("post(input", {"post": {"id": "P1", "status": "sending"}}),
        ("post(input", {"post": {"id": "P1", "status": "sent", "externalLink": "https://tiktok.com/@_takwen/video/1"}}),
    ])
    pub = BufferPublisher(services, host, session=s)
    c = ctx("takwin", "main", "tiktok", {"text": "دعاء"})
    out = pub.publish(c)
    assert out.status == "checking" and out.external_id == "P1|77"
    create = s.calls[3]["variables"]["input"]
    assert create["channelId"] == "CH_TAK" and create["mode"] == "shareNow" and create["text"] == "دعاء"
    assert create["assets"][0]["video"]["url"].endswith(".mp4")
    assert pub.check(c, delivery(out.external_id)).status == "checking"
    done = pub.check(c, delivery(out.external_id))
    assert done.status == "sent" and done.url.startswith("https://tiktok.com") and host.deleted == ["77"]
    assert state.kv_get("buffer:tiktok_acct:channel:_takwen") == "ORG|CH_TAK"
    assert state.buffer_requests_since("tiktok_acct", NOW - timedelta(minutes=1)) == 6


def test_youtube_short_payload_and_channel_with_trailing_space(setup):
    services, state, notifier, ctx = setup
    s = GraphQLSession([
        ("account", ACCOUNT), ("channels", YT_CHANNELS), ("posts(", {"posts": {"edges": []}}),
        ("createPost", {"createPost": {"__typename": "PostActionSuccess", "post": {"id": "Y1", "status": "sending"}}}),
    ])
    pub = BufferPublisher(services, FakeHost(), session=s)
    out = pub.publish(ctx("takwin", "main", "youtube", {"title": "عنوان", "description": "وصف"}))
    assert out.status == "checking"
    payload = s.calls[3]["variables"]["input"]
    assert payload["channelId"] == "YT_TAK" and payload["text"] == "وصف"
    assert payload["metadata"]["youtube"]["title"] == "عنوان" and payload["metadata"]["youtube"]["privacy"] == "public"


def test_landscape_video_goes_to_direct_youtube_fallback(setup):
    services, state, notifier, ctx = setup
    direct = FakeYoutubeDirect()
    pub = BufferPublisher(services, FakeHost(), direct_youtube=direct, session=GraphQLSession([]))
    out = pub.publish(ctx("takwin", "main", "youtube", {"title": "t", "description": "d"},
                          info=MediaInfo(1920, 1080, 40)))
    assert out.status == "manual" and out.provider == "direct" and "landscape" in direct.calls[0]


def test_buffer_error_hands_video_over_on_telegram(setup):
    services, state, notifier, ctx = setup
    host = FakeHost()
    s = GraphQLSession([
        ("account", ACCOUNT), ("channels", CHANNELS), ("posts(", {"posts": {"edges": []}}),
        ("createPost", {"createPost": {"__typename": "InvalidInputError", "message": "video too short"}}),
    ])
    pub = BufferPublisher(services, host, session=s)
    out = pub.publish(ctx("kalima_sawaa", "short", "tiktok", {"text": "كلام"}))
    assert out.status == "manual" and "video too short" in out.error
    assert host.deleted in ([77], ["77"])
    assert any("Buffer could not publish" in m for m in notifier.sent) and "كلام" in notifier.sent


def test_post_error_status_hands_over(setup):
    services, state, notifier, ctx = setup
    host = FakeHost()
    s = GraphQLSession([("post(input", {"post": {"id": "P2", "status": "error", "error": {"message": "TikTok rejected"}}})])
    pub = BufferPublisher(services, host, session=s)
    out = pub.check(ctx("takwin", "main", "tiktok", {"text": "x"}), delivery("P2|88"))
    assert out.status == "manual" and "TikTok rejected" in out.error and host.deleted == ["88"]


def test_queue_full_hands_over(setup):
    services, state, notifier, ctx = setup
    edges = [{"node": {"id": str(i), "status": "scheduled"}} for i in range(10)]
    s = GraphQLSession([("account", ACCOUNT), ("channels", CHANNELS), ("posts(", {"posts": {"edges": edges}})])
    out = BufferPublisher(services, FakeHost(), session=s).publish(ctx("takwin", "main", "tiktok", {"text": "x"}))
    assert out.status == "manual" and "already queued" in out.error


def test_budget_blocks_before_the_limit(tmp_path):
    state = State(tmp_path / "b.db")
    limits = BufferLimits(per_15min=100, per_day=100, per_30days=3000, max_queued=10, safety_margin=10)
    budget = BufferBudget(state, "acct", limits, lambda: NOW)
    for i in range(90):
        state.record_buffer_request("acct", NOW - timedelta(hours=2, minutes=i))
    with pytest.raises(BudgetExceeded):
        budget.take()
    assert state.buffer_requests_since("acct", NOW - timedelta(days=1)) == 90


def test_channel_matching():
    assert channel_matches({"name": "TAKWEN تكوين ", "displayName": None}, "takwen  تكوين")
    assert not channel_matches({"name": "Kalima Sawaa"}, "TAKWEN تكوين")


# ---------------------------------------------------------------- Facebook comment hiding


class GraphSession:
    def __init__(self, routes):
        self.routes = list(routes)
        self.calls = []

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        for i, (m, frag, payload) in enumerate(self.routes):
            if m == method and frag in url:
                self.routes.pop(i)
                return Resp(payload)
        raise AssertionError(f"unexpected {method} {url}")


def test_hide_comments_needs_permissions_and_alerts_once(app, tmp_path, monkeypatch):
    from autopost.platforms import meta

    state = State(tmp_path / "c.db")
    notifier = Notifier(None, None, dry_run=True)
    monkeypatch.setattr(meta, "token_scopes", lambda token, version: {"pages_manage_posts"})
    meta.hide_facebook_comments(app, state, Secrets({"META_PAGE_TOKEN_KALIMA": "t"}), notifier, NOW)
    assert any("pages_read_user_content" in m and "pages_manage_engagement" in m for m in notifier.sent)


def test_hide_comments_hides_visitor_comments_only(app, tmp_path, monkeypatch):
    from autopost.platforms import meta

    state = State(tmp_path / "c.db")
    state.record_early_delivery("kalima_sawaa", "short", "src", "facebook", "direct", "sent", "VID1",
                                "https://fb/1", {}, NOW - timedelta(days=1))
    monkeypatch.setattr(meta, "token_scopes", lambda token, version: set(meta.HIDE_SCOPES))
    session = GraphSession([
        ("GET", "VID1/comments", {"data": [
            {"id": "c1", "is_hidden": False, "can_hide": True, "from": {"id": "visitor"}},
            {"id": "c2", "is_hidden": True, "can_hide": True, "from": {"id": "visitor"}},
            {"id": "c3", "is_hidden": False, "can_hide": True, "from": {"id": "1177142762151237"}}]}),
        ("POST", "/c1", {"success": True}),
    ])
    monkeypatch.setattr(meta.requests, "Session", lambda: session)
    notifier = Notifier(None, None, dry_run=True)
    hidden = meta.hide_facebook_comments(app, state, Secrets({"META_PAGE_TOKEN_KALIMA": "t"}), notifier, NOW)
    assert hidden == {"kalima_sawaa": 1}
    assert session.calls[-1][2]["data"] == {"is_hidden": "true"}
    # within the interval nothing is swept again
    assert meta.hide_facebook_comments(app, state, Secrets({"META_PAGE_TOKEN_KALIMA": "t"}), notifier,
                                       NOW + timedelta(minutes=5)) == {}


def test_slow_post_is_removed_from_buffer_and_handed_over(setup):
    services, state, notifier, ctx = setup
    host = FakeHost()
    created = (NOW - timedelta(minutes=25)).isoformat().replace("+00:00", "Z")
    s = GraphQLSession([
        ("post(input", {"post": {"id": "P1", "status": "sending", "createdAt": created}}),
        ("deletePost", {"deletePost": {"__typename": "DeletePostSuccess"}}),
    ])
    pub = BufferPublisher(services, host, session=s)
    out = pub.check(ctx("takwin", "main", "tiktok", {"text": "t"}), delivery("P1|77"))
    assert out.status == "manual" and "removed from Buffer" in out.error and host.deleted == ["77"]


def test_post_buffer_is_still_sending_is_not_handed_over_twice(setup):
    services, state, _, ctx = setup

    class TelegramOK:
        def post(self, url, data=None, files=None, timeout=None):
            return Resp({"ok": True, "result": {}})

    notifier = services.notifier = Notifier("tok", "chat", session=TelegramOK())
    host = FakeHost()
    c = ctx("kalima_sawaa", "short", "tiktok", {"text": "t"})
    not_allowed = {"deletePost": {"__typename": "NotFoundError", "message": "Account is not allowed to perform this action on post"}}

    def sending(minutes):
        created = (NOW - timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")
        return ("post(input", {"post": {"id": "P1", "status": "sending", "createdAt": created}})

    pub = BufferPublisher(services, host, session=GraphQLSession([sending(25), ("deletePost", not_allowed),
                                                                  sending(35), ("deletePost", not_allowed)]))
    first = pub.check(c, delivery("P1|77"))
    second = pub.check(c, delivery("P1|77"))
    assert first.status == second.status == "checking" and host.deleted == []
    assert first.check_after == NOW + timedelta(minutes=10)
    assert sum("do not post it by hand" in m for m in notifier.sent) == 1          # one alert, not one per check

    pub = BufferPublisher(services, host, session=GraphQLSession([sending(181), ("deletePost", not_allowed)]))
    last = pub.check(c, delivery("P1|77"))
    assert last.status == "manual" and "may still appear" in last.error and host.deleted == ["77"]

    pub = BufferPublisher(services, host, session=GraphQLSession([
        ("post(input", {"post": {"id": "P1", "status": "sent", "externalLink": "https://tiktok.com/v/9"}})]))
    assert pub.check(c, delivery("P1|77")).status == "sent"
