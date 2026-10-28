"""Kalima short folder filled by tg2drive: newest-first order, backfill stats, captionless files, media policy."""

from datetime import date, datetime, timedelta, timezone

from autopost.content.base import VideoItem
from autopost.content.drive_source import choose_next_row
from autopost.content.google_api import created_order
from autopost.media import EncodeSettings, MediaInfo, transcode_reasons, video_filter

from test_drive_source import FakeClient, make, rows

UTC = timezone.utc
SHORT = "1qf_opI5CAtrP5iDn3goNxAZY23rj7Z4c"
BASE = datetime(2026, 10, 28, 6, 0, tzinfo=UTC)


def reel(msg, name=None, minute=None):
    return VideoItem(id=f"m{msg}", name=name or f"caption {msg} with 3 numbers.mp4",
                     created=BASE + timedelta(minutes=msg if minute is None else minute), size=50_000_000,
                     app_properties={"source": "tg2drive", "tg_key": f"-100123:{msg}"})


def fill(client, items):
    client.files[SHORT] = sorted(items, key=created_order)


_days = iter(range(1000))


def post(state, video_id, slot="morning"):
    day = date(2026, 10, 28) + timedelta(days=next(_days))     # one post per (day, slot)
    state.create_post("kalima_sawaa", "short", day, slot, video_id, video_id, None, None, {},
                      [("facebook", "direct")], BASE)


def small_backfill(app, size=3):
    app.brands["kalima_sawaa"].streams["short"].backfill.size = size


def test_created_time_ties_broken_by_msg_id_not_name_or_numbers():
    a = reel(20, name="1 first by name.mp4", minute=0)
    b = reel(7, name="9 later by name.mp4", minute=0)
    c = reel(5, minute=-1)
    assert [v.id for v in sorted([a, b, c], key=created_order)] == ["m5", "m7", "m20"]
    assert VideoItem(id="x", name="x", created=None).tg_msg_id is None


def test_every_slot_posts_the_newest_unposted_reel(app, tmp_path):
    app.brands["kalima_sawaa"].streams["short"].order = "newest_first"
    content, state, client = make(app, tmp_path)
    # uploaded oldest first by the backfill; a newer reel uploaded earlier still wins on msg_id
    fill(client, [reel(10), reel(11), reel(12), reel(40, minute=-5)])
    order = []
    for slot in ("morning", "afternoon", "evening", "night"):
        v = content.next_video("kalima_sawaa", "short", slot=slot)
        order.append(v.id)
        post(state, v.id, slot)
    assert order == ["m40", "m12", "m11", "m10"]
    assert content.next_video("kalima_sawaa", "short", slot="morning") is None


def test_backfill_stats_for_the_daily_summary(app, tmp_path):
    small_backfill(app)
    content, state, client = make(app, tmp_path)
    fill(client, [reel(10), reel(11)])
    s = content.stream_stats("kalima_sawaa", "short", BASE.date())
    assert (s["backfill_done"], s["queue"], s["queue_new"], s["in_folder"]) == (False, 2, 0, 2)

    content, state, client = make(app, tmp_path / "b")
    fill(client, [reel(10), reel(11), reel(12), reel(30), reel(31, minute=24 * 60)])
    post(state, "m31")
    s = content.stream_stats("kalima_sawaa", "short", BASE.date())
    assert state.kv_get("backfill:kalima_sawaa:short") == "12"
    assert (s["backfill_done"], s["queue"], s["queue_new"]) == (True, 4, 1)
    assert (s["received"], s["received_new"]) == (4, 1)          # m31 arrived the next day
    # the boundary stays fixed when backfill files are deleted; config can override it
    content, _, client = make(app, tmp_path / "b")
    fill(client, [reel(11), reel(30), reel(31)])
    assert content.backfill_boundary("kalima_sawaa", "short") == 12
    app.brands["kalima_sawaa"].streams["short"].backfill.boundary_msg_id = 30
    assert content.backfill_boundary("kalima_sawaa", "short") == 30


def test_captionless_file_is_skipped_with_one_alert(app, tmp_path):
    content, state, client = make(app, tmp_path)
    sent = []
    content.notify = lambda text, key: sent.append(key)
    fill(client, [reel(10, name="معاذ عليان.mp4"), reel(11)])
    assert content.next_video("kalima_sawaa", "short", slot="morning").id == "m11"
    assert content.remaining("kalima_sawaa", "short") == 1
    content.next_video("kalima_sawaa", "short", slot="night")
    assert sent == ["skip-title:kalima_sawaa:m10"]


def test_slash_restored_other_characters_kept(app, tmp_path):
    content, _, _ = make(app, tmp_path)
    v = VideoItem(id="x", name="هل هو إله؟ نعم/لا ⧸ القس: يوحنا | الجزء 2.mp4", created=None)
    assert content.pick_text("kalima_sawaa", "short", v).variables == {"title": "هل هو إله؟ نعم/لا / القس: يوحنا | الجزء 2"}


def test_posts_of_deleted_files_do_not_change_counts(app, tmp_path):
    app.brands["kalima_sawaa"].streams["short"].order = "created_time"
    content, state, client = make(app, tmp_path)
    for old in ("deleted-1", "deleted-2", "deleted-3"):
        post(state, old)
    fill(client, [reel(10), reel(11)])
    assert content.remaining("kalima_sawaa", "short") == 2
    assert content.next_video("kalima_sawaa", "short", slot="morning").id == "m10"      # oldest first


def test_takwin_rotation_starts_at_configured_row(app):
    assert app.brands["takwin"].text_source.start_row == 3
    assert choose_next_row(rows(88), start_row=3).number == 3
    assert choose_next_row(rows(88, posted={3: BASE}), start_row=3).number == 4


def test_kalima_short_media_policy(app, tmp_path):
    content, _, client = make(app, tmp_path)
    fill(client, [reel(10)])
    content.listing("kalima_sawaa", "short")
    enc = content.encode_for(reel(10))
    assert (enc.transcode, enc.upscale) == ("codec_only", False)
    assert content.encode_for(VideoItem(id="other", name="1.mp4", created=None)).transcode == "spec"

    hq = MediaInfo(1080, 1920, 60, container="mov,mp4,m4a", vcodec="h264", pix_fmt="yuv420p", acodec="aac",
                   fps=60.0, faststart=False)
    assert transcode_reasons(hq, enc) == []
    assert transcode_reasons(hq, EncodeSettings()) != []
    hevc = MediaInfo(1080, 1920, 60, container="mov,mp4,m4a", vcodec="hevc", pix_fmt="yuv420p10le", acodec="aac")
    assert transcode_reasons(hevc, enc) == ["video codec hevc"]
    small = MediaInfo(240, 426, 20, container="mov,mp4,m4a", vcodec="av1", pix_fmt="yuv420p", acodec="aac")
    vf, box = video_filter(small, enc)
    assert box is None and "pad=" not in vf


def test_rupload_retry_resends_the_file_from_the_offset(tmp_path, monkeypatch):
    import autopost.platforms.meta as meta

    class Resp:
        def __init__(self, status, body):
            self.status_code, self._body, self.text = status, body, str(body)

        def json(self):
            return self._body

    class Session:
        def __init__(self):
            self.bodies = []

        def post(self, url, data=None, headers=None, timeout=None):
            self.bodies.append(data if isinstance(data, bytes) else data.read())
            return Resp(500, {"error": {"message": "busy"}}) if len(self.bodies) == 1 else Resp(200, {"success": True})

    monkeypatch.setattr(meta.time, "sleep", lambda s: None)
    f = tmp_path / "v.mp4"
    f.write_bytes(b"0123456789")
    for limit in (1 << 30, 0):                       # in memory, then streamed from disk
        monkeypatch.setattr(meta, "MAX_IN_MEMORY", limit)
        s = Session()
        meta.GraphClient("t", "v26.0", session=s).rupload("https://rupload", f, offset=4)
        assert s.bodies == [b"456789", b"456789"]


def test_telegram_fallback_over_50_mb_sends_the_drive_link(tmp_path):
    from autopost.alerts.telegram import Notifier

    class Session:
        def __init__(self):
            self.calls = []

        def post(self, url, data=None, files=None, timeout=None):
            self.calls.append((url.rsplit("/", 1)[-1], data, files))

            class R:
                status_code = 200

                def json(self):
                    return {"ok": True, "result": {}}
            return R()

    big = tmp_path / "reel.mp4"
    with big.open("wb") as fh:
        fh.truncate(85 * 1024 * 1024)               # sparse 85 MB file
    s = Session()
    assert Notifier("tok", "chat", session=s).send_video(big, "caption", fallback_link="https://drive/x")
    assert [c[0] for c in s.calls] == ["sendMessage"]
    assert "https://drive/x" in s.calls[0][1]["text"] and "85 MB" in s.calls[0][1]["text"]
