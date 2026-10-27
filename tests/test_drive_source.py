from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from autopost.content.base import VideoItem
from autopost.content.drive_source import (
    ContentError,
    DriveContent,
    SheetRow,
    choose_next_row,
    column_letter,
    parse_rows,
)
from autopost.content.google_api import GoogleClient, GoogleError
from autopost.state import State

UTC = timezone.utc


def rows(n, posted=None, empty=()):
    posted = posted or {}
    return [SheetRow(r, {"caption": "" if r in empty else f"c{r}", "title": f"t{r}"}, posted.get(r))
            for r in range(2, n + 2)]


def t(h):
    return datetime(2026, 10, 27, h, tzinfo=UTC)


def test_rotation_starts_at_first_data_row():
    assert choose_next_row(rows(88)).number == 2


def test_rotation_follows_latest_posted_date_not_position():
    r = rows(88, posted={10: t(5), 30: t(9), 4: t(7)})
    assert choose_next_row(r).number == 31


def test_rotation_wraps_after_last_row():
    assert choose_next_row(rows(88, posted={89: t(9), 2: t(1)})).number == 2


def test_rotation_skips_incomplete_rows():
    assert choose_next_row(rows(10, posted={3: t(9)}, empty={4, 5})).number == 6
    with pytest.raises(ContentError):
        choose_next_row(rows(3, empty={2, 3, 4}))


def test_state_cursor_used_when_newer_than_sheet():
    r = rows(88, posted={10: t(5)})
    assert choose_next_row(r, cursor=(20, t(6))).number == 21     # sheet write failed last time
    assert choose_next_row(r, cursor=(20, t(4))).number == 11     # someone edited the sheet later


def test_parse_rows_header_offset_and_short_rows(app):
    src = app.brands["takwin"].text_source
    raw = [["cap", "tit", "2026-10-27 05:20:00"], ["cap2", "tit2"], ["only caption"]]
    parsed = parse_rows(raw, src, app.settings.tz)
    assert [r.number for r in parsed] == [2, 3, 4]
    assert parsed[0].posted_at == datetime(2026, 10, 27, 5, 20, tzinfo=app.settings.tz)
    assert parsed[1].posted_at is None and parsed[2].values == {"caption": "only caption", "title": ""}
    assert column_letter(0) == "A" and column_letter(26) == "AA"


class FakeClient:
    def __init__(self):
        self.writes = []
        base = datetime(2026, 7, 16, 9, 59, tzinfo=UTC)
        self.files = {
            "1YFMErkAWf1WvmY547qbShBk0DT5lSBQR": [
                VideoItem(id=f"v{i}", name=f"{i}.mp4", created=base + timedelta(minutes=i), size=10)
                for i in (3, 1, 2)],
        }
        self.sheet = [["c2", "t2", ""], ["c3", "t3", "2026-10-27 05:20:00"], ["c4", "t4", ""]]

    def list_videos(self, folder):
        return sorted(self.files.get(folder, []), key=lambda v: v.created)

    def get_video(self, fid):
        raise GoogleError("gone")

    def sheet_title(self, sid, index):
        return "Untitled"

    def read_range(self, sid, a1):
        assert a1 == "'Untitled'!A2:C"
        return self.sheet

    def write_cell(self, sid, a1, value):
        self.writes.append((a1, value))

    def download(self, fid, dest, size):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"x" * 10)
        return dest


def make(app, tmp_path, dry_run=False):
    state = State(tmp_path / "s.db")
    client = FakeClient()
    return DriveContent(app, state, client, tmp_path / "media", dry_run), state, client


def test_videos_oldest_first_and_used_ones_skipped(app, tmp_path):
    content, state, _ = make(app, tmp_path)
    assert content.next_video("takwin", "main").id == "v1"
    state.create_post("takwin", "main", datetime(2026, 10, 27).date(), "fajr", "v1", "1.mp4", None, None, {},
                      [("facebook", "direct")], t(1))
    assert content.next_video("takwin", "main").id == "v2"
    assert content.remaining("takwin", "main") == 2


def test_sheet_rotation_writes_posted_date_and_advances(app, tmp_path):
    content, state, client = make(app, tmp_path)
    video = content.next_video("takwin", "main")
    first = content.pick_text("takwin", "main", video)
    assert first.row == 4 and first.variables == {"caption": "c4", "title": "t4"}
    when = datetime(2026, 10, 27, 15, 2, 3, tzinfo=UTC)
    content.commit_text("takwin", first, when)
    assert client.writes == [("'Untitled'!C4", "2026-10-27 18:02:03")]      # local Cairo time (UTC+3)
    assert content.pick_text("takwin", "main", video).row == 2               # wrapped
    assert state.caption_cursor("takwin")[0] == 4


def test_dry_run_never_writes_the_sheet_but_still_rotates(app, tmp_path):
    content, state, client = make(app, tmp_path, dry_run=True)
    video = content.next_video("takwin", "main")
    a = content.pick_text("takwin", "main", video)
    content.commit_text("takwin", a, t(16))
    b = content.pick_text("takwin", "main", video)
    assert client.writes == [] and (a.row, b.row) == (4, 2)


def test_filename_titles_for_kalima(app, tmp_path):
    content, _, _ = make(app, tmp_path)
    v = VideoItem(id="x", name="Copy of حوار ناري：القس.mp4", created=None)
    assert content.pick_text("kalima_sawaa", "long", v).variables == {"title": "حوار ناري:القس"}


def test_fetch_and_release(app, tmp_path, monkeypatch):
    from autopost.media import MediaInfo

    def fake_prepare(src, out_dir, settings):
        out = out_dir / f"{src.stem}.prepared.mp4"
        out.write_bytes(b"y")
        return out, MediaInfo(240, 426, 5, vcodec="av1"), MediaInfo(720, 1280, 5, vcodec="h264")

    monkeypatch.setattr("autopost.content.drive_source.prepare_for_upload", fake_prepare)
    content, _, _ = make(app, tmp_path)
    v = content.next_video("takwin", "main")
    v.local_path = content.fetch(v)
    assert Path(v.local_path).name.endswith(".prepared.mp4") and content.fetch(v) == v.local_path
    assert content.probe(v).width == 720
    content.release(v)
    assert v.local_path is None and not any((tmp_path / "media").iterdir())


# ---------------------------------------------------------------- REST client


class FakeResponse:
    def __init__(self, status, payload=None, body=b""):
        self.status_code = status
        self._payload = payload or {}
        self._body = body
        self.text = str(payload)

    def json(self):
        return self._payload

    def iter_content(self, chunk_size):
        yield self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        return self.responses.pop(0)

    def get(self, url, **kw):
        return self.request("GET", url, **kw)


def test_list_videos_query_pagination_and_retry(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    f = {"id": "a", "name": "1.mp4", "createdTime": "2026-07-16T09:59:23.099Z", "size": "2544740",
         "videoMediaMetadata": {"width": 1080, "height": 1920, "durationMillis": "45000"}}
    g = dict(f, id="b", name="0.mp4", createdTime="2026-07-16T09:59:01.640Z")
    session = FakeSession([FakeResponse(503), FakeResponse(200, {"files": [f], "nextPageToken": "p2"}),
                           FakeResponse(200, {"files": [g]})])
    items = GoogleClient(session).list_videos("FOLDER")
    assert [i.id for i in items] == ["b", "a"]
    assert items[1].width == 1080 and items[1].duration_ms == 45000 and items[1].size == 2544740
    q = session.calls[0][2]["params"]["q"]
    assert q == "'FOLDER' in parents and trashed = false and mimeType contains 'video/'"
    assert session.calls[2][2]["params"]["pageToken"] == "p2"


def test_http_error_message_is_reported():
    session = FakeSession([FakeResponse(404, {"error": {"message": "File not found: X."}})])
    with pytest.raises(GoogleError, match="File not found"):
        GoogleClient(session).sheet_title("X", 0)


def test_download_size_mismatch_retries_then_fails(tmp_path, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    session = FakeSession([FakeResponse(200, body=b"abc") for _ in range(4)])
    with pytest.raises(GoogleError, match="incomplete"):
        GoogleClient(session).download("id", tmp_path / "v.mp4", expected_size=10)
    assert not list(tmp_path.iterdir())


def test_order_by_name_number():
    from autopost.content.drive_source import name_number, order_by_name_number

    base = datetime(2026, 10, 26, 10, tzinfo=UTC)
    names = ["68.mp4", "9.mp4", "293.mp4", "intro.mp4", "10.mp4", "10b.mp4", "2.mp4", "outro.mp4", "clip 2.mp4"]
    items = [VideoItem(id=f"i{n}", name=name, created=base + timedelta(minutes=-n)) for n, name in enumerate(names)]
    ordered, unnumbered = order_by_name_number(items)
    assert [v.name for v in ordered] == ["2.mp4", "clip 2.mp4", "9.mp4", "10.mp4", "10b.mp4", "68.mp4", "293.mp4",
                                         "outro.mp4", "intro.mp4"]          # unnumbered: oldest created first
    assert {v.name for v in unnumbered} == {"intro.mp4", "outro.mp4"}
    assert name_number("Copy of 7 حلقة.mp4") == 7 and name_number("بدون.mp4") is None


def test_takwin_uses_name_order_and_warns_once_per_set(app, tmp_path):
    warnings = []
    content, state, client = make(app, tmp_path)
    content.notify = lambda text, key: warnings.append((text, key))
    base = datetime(2026, 10, 26, 10, tzinfo=UTC)
    client.files["1YFMErkAWf1WvmY547qbShBk0DT5lSBQR"] = [
        VideoItem(id="a", name="68.mp4", created=base), VideoItem(id="b", name="9.mp4", created=base),
        VideoItem(id="c", name="extra.mp4", created=base)]
    assert [v.name for v in content.listing("takwin", "main")] == ["9.mp4", "68.mp4", "extra.mp4"]
    assert len(warnings) == 1 and "extra.mp4" in warnings[0][0]
    assert warnings[0][1].startswith("no-number:takwin:main:")
