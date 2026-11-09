"""Telegram control: buttons for publish now / skip / pause, and the bot's own plumbing."""

import json
from argparse import Namespace

import pytest

from autopost.alerts.telegram import Notifier
from autopost.control import OFFSET, PAUSED, Controller

from test_audio_check import setup

CHAT = "42"


class FakeNotifier:
    chat_id = CHAT

    def __init__(self):
        self.sent, self.edits, self.answers = [], [], []

    def send(self, text, buttons=None):
        self.sent.append((text, buttons))
        return True

    def edit(self, message_id, text, buttons=None):
        self.edits.append((message_id, text, buttons))
        return True

    def answer_callback(self, callback_id, text=""):
        self.answers.append(text)


def controller(app, tmp_path, **kw):
    content, state, client, backend, transcriber, alerts = setup(app, tmp_path, **kw)
    notifier, dispatched, changes = FakeNotifier(), [], []

    def dispatch(brand, stream, platforms, video_id):
        dispatched.append((brand, stream, platforms, video_id))
        return None

    ctl = Controller(app, state, content, notifier, dispatch, on_change=lambda: changes.append(1))
    return ctl, notifier, dispatched, changes, state, content


def press(ctl, data, chat=CHAT, message_id=7):
    ctl.handle({"update_id": 1, "callback_query": {"id": "cb1", "data": data,
                                                    "message": {"message_id": message_id, "chat": {"id": int(chat)}}}})


def say(ctl, text, chat=CHAT):
    ctl.handle({"update_id": 1, "message": {"text": text, "chat": {"id": int(chat)}}})


def test_only_the_configured_chat_is_obeyed(app, tmp_path):
    ctl, notifier, dispatched, changes, *_ = controller(app, tmp_path)
    say(ctl, "/menu", chat="999")
    press(ctl, "p:m3", chat="999")
    press(ctl, "pause", chat="999")
    assert notifier.sent == [] and notifier.edits == [] and dispatched == [] and changes == []


def test_menu_shows_the_next_video_with_buttons(app, tmp_path):
    ctl, notifier, *_ = controller(app, tmp_path)
    say(ctl, "/menu")
    cards = [(t, b) for t, b in notifier.sent if b and b[0][0][1].startswith("p:") and "كلمة سواء" in t]
    assert len(cards) == 1
    text, buttons = cards[0]
    assert "عنوان 3" in text and "كلمة سواء" in text and "short" in text
    assert [d for row in buttons for _, d in row] == ["p:m3", "c:m3", "s:m3"]
    assert all(len(d.encode()) <= 64 for row in buttons for _, d in row)
    assert "النشر التلقائي شغال" in notifier.sent[-1][0]


def test_publish_now_dispatches_every_enabled_platform_and_confirms(app, tmp_path):
    ctl, notifier, dispatched, *_ = controller(app, tmp_path)
    press(ctl, "p:m3")
    brand, stream, platforms, vid = dispatched[0]
    assert (brand, stream, vid) == ("kalima_sawaa", "short", "m3")
    assert set(platforms.split(",")) == {"facebook", "instagram", "tiktok", "youtube"}      # pinterest is disabled
    assert notifier.edits[0][0] == 7 and "بدأ النشر" in notifier.edits[0][1] and notifier.edits[0][2] is None


def test_one_platform_chooser_and_publish(app, tmp_path):
    ctl, notifier, dispatched, *_ = controller(app, tmp_path)
    press(ctl, "c:m3")
    labels = [label for row in notifier.edits[0][2] for label, _ in row]
    assert "تيك توك" in labels and "رجوع" in labels
    press(ctl, "q:m3:t")
    assert dispatched == [("kalima_sawaa", "short", "tiktok", "m3")]


def test_a_dispatch_failure_is_reported_not_hidden(app, tmp_path):
    ctl, notifier, *_ = controller(app, tmp_path)
    ctl.dispatcher = lambda *a: "GitHub answered HTTP 403"
    press(ctl, "p:m3")
    assert "مقدرتش أبدأ النشر" in notifier.edits[0][1] and "403" in notifier.edits[0][1]


def test_skip_removes_the_video_from_the_queue_and_can_be_undone(app, tmp_path):
    ctl, notifier, dispatched, changes, state, content = controller(app, tmp_path)
    press(ctl, "s:m3")
    assert changes == [1] and state.kv_get("ctl:skip:m3") == "1"
    assert content.unused("kalima_sawaa", "short")[0].id == "m2"
    assert content.next_video("kalima_sawaa", "short").id == "m2"
    press(ctl, "u:m3")
    assert content.unused("kalima_sawaa", "short")[0].id == "m3"
    assert changes == [1, 1]


def test_pause_and_resume_buttons(app, tmp_path):
    ctl, notifier, _, changes, state, _ = controller(app, tmp_path)
    press(ctl, "pause")
    assert state.kv_get(PAUSED) == "1" and "موقوف" in notifier.edits[-1][1]
    assert notifier.edits[-1][2][0][0][1] == "resume"
    say(ctl, "/resume")
    assert not state.kv_get(PAUSED) and "شغال" in notifier.sent[-1][0] and len(changes) == 2


def test_a_video_that_is_gone_gets_a_clear_answer(app, tmp_path):
    ctl, notifier, dispatched, *_ = controller(app, tmp_path)
    press(ctl, "p:does-not-exist")
    assert dispatched == [] and "لم يعد في الطابور" in notifier.edits[0][1]


def test_a_crash_inside_a_command_is_reported_and_survived(app, tmp_path):
    ctl, notifier, *_ = controller(app, tmp_path)
    ctl.menu = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    say(ctl, "/menu")
    assert "تعذّر تنفيذ الأمر" in notifier.sent[-1][0] and "boom" in notifier.sent[-1][0]


def test_the_card_shows_the_replacement_title_of_a_blocked_title(app, tmp_path):
    ctl, notifier, dispatched, changes, state, content = controller(app, tmp_path)
    backend = content.guard_for("kalima_sawaa").backend
    backend.titles["عنوان 3"] = {"title": "", "risk": "high", "reasons": ["insult"]}
    assert content.next_video("kalima_sawaa", "short").id == "m3"           # checked: retitled
    say(ctl, "/menu")
    card = next(t for t, b in notifier.sent if b and b[0][0][1] == "p:m3")
    assert "قراءة في الموضوع" in card and "الفحص: ✅" in card


def test_a_scheduled_run_does_nothing_while_paused(app, tmp_path, monkeypatch):
    from autopost import __main__ as cli
    from autopost.state import State

    db = tmp_path / "state.db"
    s = State(db)
    s.kv_set(PAUSED, "1")
    s.close()
    monkeypatch.setattr(cli, "maintenance", lambda *a, **k: None)
    monkeypatch.setenv("AUTOPOST_TRIGGER", "cron")
    app.settings.cron_enabled = True
    code, summary = cli._run(Namespace(dry_run=False, state=str(db), now=None, real_sleep=False, brand=None,
                                       max_runtime_minutes=5, download=False, placeholder_content=True), app)
    assert code == 0 and "paused" in summary


# ------------------------------------------------------------------ the Telegram side

class Session:
    def __init__(self, reply=None):
        self.posts, self.reply = [], reply or {"ok": True, "result": []}

    def post(self, url, data=None, files=None, timeout=None):
        self.posts.append((url.rsplit("/", 1)[-1], data))

        class R:
            status_code = 200
            text = "{}"

            def json(inner):
                return self.reply

        return R()


def test_buttons_are_sent_as_an_inline_keyboard():
    session = Session()
    n = Notifier("T", CHAT, session=session)
    n.send("hello", [[("نشر", "p:1"), ("تخطي", "s:1")], [("إيقاف", "pause")]])
    method, data = session.posts[0]
    assert method == "sendMessage"
    keyboard = json.loads(data["reply_markup"])["inline_keyboard"]
    assert keyboard[0][0] == {"text": "نشر", "callback_data": "p:1"} and keyboard[1][0]["callback_data"] == "pause"
    n.send("plain")
    assert "reply_markup" not in session.posts[1][1]


def test_get_updates_edit_and_answer_use_the_right_methods():
    session = Session({"ok": True, "result": [{"update_id": 5}]})
    n = Notifier("T", CHAT, session=session)
    assert n.get_updates(5, 20) == [{"update_id": 5}]
    n.answer_callback("cb", "تم")
    n.edit(9, "new", [[("x", "y")]])
    assert [m for m, _ in session.posts] == ["getUpdates", "answerCallbackQuery", "editMessageText"]
    assert session.posts[0][1]["offset"] == 5 and session.posts[2][1]["message_id"] == 9
    assert Notifier("T", CHAT, dry_run=True).get_updates(0, 5) == []


def test_a_publishing_runs_stale_copy_never_undoes_a_telegram_control(tmp_path):
    from autopost.state import State, merge_states

    theirs, ours = State(tmp_path / "t.db"), State(tmp_path / "o.db")
    theirs.kv_set(PAUSED, "")                  # resumed from Telegram meanwhile
    ours.kv_set(PAUSED, "1")                   # the publisher started while it was paused
    theirs.close(), ours.close()
    merge_states(tmp_path / "t.db", tmp_path / "o.db", tmp_path / "m.db")
    assert State(tmp_path / "m.db").kv_get(PAUSED) == ""
    merge_states(tmp_path / "t.db", tmp_path / "o.db", tmp_path / "m2.db", kv_ours_prefixes=("ctl:",))
    assert State(tmp_path / "m2.db").kv_get(PAUSED) == "1"      # the control job itself wins


def test_every_platform_caption_credits_moaz_with_the_title_first():
    from autopost.config import load_config
    from autopost.content.text import render
    from pathlib import Path

    app = load_config(Path(__file__).resolve().parent.parent / "config")
    brand = app.brands["kalima_sawaa"]
    for platform in ("facebook", "instagram", "tiktok", "youtube"):
        texts = render(brand.templates[platform], platform, {"title": "قراءة في موضوع"}, app.settings.text_limits)
        body = texts.get("text") or texts["description"]
        assert body.startswith("قراءة في موضوع") and "#معاذ_عليان" in body and "#كلمة_سواء" in body
    assert render(brand.templates["facebook"], "facebook", {"title": "x"}, app.settings.text_limits)["text"].count("#") <= 5
