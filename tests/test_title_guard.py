import json
from datetime import datetime, timezone

import pytest
import requests

from autopost.content.base import VideoItem
from autopost.content.drive_source import ContentError
from autopost.title_guard import GuardError, TitleGuard, Verdict, clean_obfuscation, parse_reply

from test_drive_source import AnthropicResponse, EchoAnthropic, make

SHORT = "1qf_opI5CAtrP5iDn3goNxAZY23rj7Z4c"
BASE = datetime(2026, 11, 1, 9, tzinfo=timezone.utc)


def test_dots_and_tatweel_inside_words_are_removed():
    assert clean_obfuscation("البرمـ..يل أثناسيوس فهمي واز.د.راء الإسلام يط..عن بكل وقـ..ـاحة") == \
        "البرميل أثناسيوس فهمي وازدراء الإسلام يطعن بكل وقاحة"


def test_a_normal_abbreviation_dot_is_kept():
    assert clean_obfuscation("رأي د.محمد في الآية 3.14") == "رأي د.محمد في الآية 3.14"


def test_low_risk_keeps_the_original_even_if_the_model_changed_it():
    v = parse_reply('{"title": "غيّرته", "risk": "low", "reasons": []}', "العنوان الأصلي")
    assert (v.title, v.risk) == ("العنوان الأصلي", "low")


def test_medium_uses_the_rewrite_and_trims_it():
    v = parse_reply('prefix {"title": "قراءة في نص " , "risk": "MEDIUM", "reasons": ["mockery"]} suffix', "x")
    assert (v.title, v.risk, v.reasons) == ("قراءة في نص", "medium", ("mockery",))
    long = parse_reply(json.dumps({"title": "كلمة " * 60, "risk": "medium"}), "x")
    assert len(long.title) <= 100


def test_high_needs_no_title_and_bad_replies_are_errors():
    assert parse_reply('{"risk": "high", "reasons": ["insult"]}', "الأصلي").title == "الأصلي"
    for bad in ("no json here", '{"title": "t", "risk": "unknown"}', '{"title": "", "risk": "medium"}', "{oops}"):
        with pytest.raises(GuardError):
            parse_reply(bad, "x")


def test_judge_cleans_the_title_before_sending_and_retries_overload():
    sleeps = []

    class Flaky(EchoAnthropic):
        def post(self, *a, **kw):
            if len(self.calls) == 0:
                self.calls.append("overloaded")
                return AnthropicResponse({}, 529)
            return super().post(*a, **kw)

    session = Flaky()
    verdict = TitleGuard("k", "claude-sonnet-5-5", session=session, sleep=sleeps.append).judge("واز.د.راء الإسلام")
    assert verdict.title == "وازدراء الإسلام" and session.calls == ["overloaded", "وازدراء الإسلام"]
    assert sleeps == [2]


def test_judge_fails_closed_on_a_rejected_key_or_network_error():
    with pytest.raises(GuardError, match="HTTP 401"):
        TitleGuard("bad", "m", session=EchoAnthropic(status=401), sleep=lambda s: None).judge("t")

    class Down:
        def post(self, *a, **kw):
            raise requests.ConnectionError()

    with pytest.raises(GuardError, match="network"):
        TitleGuard("k", "m", session=Down(), sleep=lambda s: None).judge("t")


def reel(n, name):
    return VideoItem(id=f"m{n}", name=name, created=BASE, app_properties={"tg_key": f"-100:{n}"})


def test_high_risk_video_is_held_back_and_the_next_one_is_used(app, tmp_path):
    guard = EchoAnthropic({"عنوان مسيء": {"title": "", "risk": "high", "reasons": ["insult"]}})
    content, state, client = make(app, tmp_path, guard=guard)
    app.brands["kalima_sawaa"].streams["short"].order = "newest_first"
    client.files[SHORT] = [reel(1, "عنوان هادئ.mp4"), reel(2, "عنوان مسيء.mp4")]
    alerts = []
    content.notify = lambda text, key: alerts.append((text, key))
    assert content.next_video("kalima_sawaa", "short").id == "m1"
    assert len(alerts) == 1 and alerts[0][1] == "guard-hold:kalima_sawaa:m2" and "insult" in alerts[0][0]
    assert content.remaining("kalima_sawaa", "short") == 1          # the held-back one is out of the queue
    content.next_video("kalima_sawaa", "short")
    assert guard.calls.count("عنوان مسيء") == 1                      # judged once, then cached in state


def test_caption_uses_the_rewritten_title(app, tmp_path):
    guard = EchoAnthropic({"مسيحي اخترع نص جديد 😂": {"title": "نص جديد في الميزان", "risk": "medium",
                                                       "reasons": ["mockery"]}})
    content, _, _ = make(app, tmp_path, guard=guard)
    v = VideoItem(id="x", name="مسيحي اخترع نص جديد 😂.mp4", created=None)
    assert content.pick_text("kalima_sawaa", "short", v).variables == {"title": "نص جديد في الميزان"}


def test_api_failure_posts_nothing(app, tmp_path, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    content, _, client = make(app, tmp_path, guard=EchoAnthropic(status=500))
    client.files[SHORT] = [reel(1, "عنوان.mp4")]
    with pytest.raises(ContentError, match="title check failed"):
        content.next_video("kalima_sawaa", "short")


def test_renaming_a_held_back_file_checks_it_again(app, tmp_path):
    guard = EchoAnthropic({"سيئ": {"title": "", "risk": "high", "reasons": ["x"]}})
    content, _, client = make(app, tmp_path, guard=guard)
    client.files[SHORT] = [reel(1, "سيئ.mp4")]
    content.notify = lambda *a: None
    with pytest.raises(ContentError, match="held back"):
        content.next_video("kalima_sawaa", "short")
    client.files[SHORT] = [reel(1, "حسن.mp4")]
    content._videos.clear()
    assert content.next_video("kalima_sawaa", "short").id == "m1"


def test_missing_api_key_is_an_error_not_a_silent_post(app, tmp_path):
    from autopost.content.drive_source import DriveContent
    from autopost.secret_store import Secrets
    from autopost.state import State

    from test_drive_source import FakeClient

    client = FakeClient()
    client.files[SHORT] = [reel(1, "عنوان.mp4")]
    cfg = app.brands["kalima_sawaa"].title_guard
    cfg.provider, cfg.model, cfg.api_key_secret = "anthropic", "claude-sonnet-5-5", "ANTHROPIC_API_KEY"
    content = DriveContent(app, State(tmp_path / "s.db"), client, tmp_path / "m", False, secrets=Secrets({}))
    with pytest.raises(ContentError, match="ANTHROPIC_API_KEY"):
        content.next_video("kalima_sawaa", "short")


def test_verdict_roundtrip():
    v = Verdict("ع", "medium", ("a",), "o")
    assert Verdict.from_json(v.to_json()) == v
