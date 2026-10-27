from datetime import datetime, timedelta, timezone

from autopost.hosting.github_release import GitHubReleaseHost


class Resp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload or {}
        self.text = str(payload)

    def json(self):
        return self._payload


class Session:
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


def test_creates_release_uploads_and_cleans(tmp_path):
    video = tmp_path / "x.mp4"
    video.write_bytes(b"v" * 10)
    old = (datetime.now(timezone.utc) - timedelta(hours=7)).isoformat().replace("+00:00", "Z")
    new = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    s = Session([
        ("GET", "/releases/tags/media-temp", Resp(404)),
        ("POST", "/repos/o/r/releases", Resp(201, {"id": 9})),
        ("POST", "uploads.github.com/repos/o/r/releases/9/assets", Resp(201, {
            "id": 5, "browser_download_url": "https://github.com/o/r/releases/download/media-temp/a.mp4"})),
        ("GET", "/releases/9/assets", Resp(200, [{"id": 1, "created_at": old}, {"id": 2, "created_at": new}])),
        ("DELETE", "/releases/assets/1", Resp(204)),
    ])
    host = GitHubReleaseHost("o/r", "tok", session=s)
    url, asset_id = host.upload(video, "kalima_sawaa-tiktok")
    assert url.endswith("/media-temp/a.mp4") and asset_id == 5
    upload = s.calls[2]
    assert upload[2]["params"]["name"].startswith("kalima_sawaa-tiktok-") and upload[2]["headers"]["Content-Type"] == "video/mp4"
    assert s.calls[1][2]["json"]["prerelease"] is True
    assert host.cleanup() == 1
