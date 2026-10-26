"""Minimal Google Drive v3 / Sheets v4 REST client authenticated with a Service Account."""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

from .base import VideoItem

log = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
]
DRIVE = "https://www.googleapis.com/drive/v3"
SHEETS = "https://sheets.googleapis.com/v4/spreadsheets"
RETRY_STATUS = {429, 500, 502, 503, 504}


class GoogleError(Exception):
    pass


def session_from_service_account(info_json: str):
    from google.auth.transport.requests import AuthorizedSession
    from google.oauth2 import service_account

    try:
        info = json.loads(info_json)
    except json.JSONDecodeError as exc:
        raise GoogleError("GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON") from exc
    creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    return AuthorizedSession(creds), info.get("client_email", "?")


class GoogleClient:
    def __init__(self, session: requests.Session, attempts: int = 4):
        self.session = session
        self.attempts = attempts

    def request(self, method: str, url: str, **kwargs) -> requests.Response:
        kwargs.setdefault("timeout", 60)
        for attempt in range(1, self.attempts + 1):
            try:
                resp = self.session.request(method, url, **kwargs)
            except requests.RequestException as exc:
                if attempt == self.attempts:
                    raise GoogleError(f"{method} {url.split('?')[0]}: {type(exc).__name__}: {exc}") from exc
                time.sleep(2 ** attempt)
                continue
            if resp.status_code in RETRY_STATUS and attempt < self.attempts:
                time.sleep(2 ** attempt)
                continue
            if resp.status_code >= 400:
                raise GoogleError(f"{method} {url.split('?')[0]} → HTTP {resp.status_code}: {_error_message(resp)}")
            return resp
        raise GoogleError("unreachable")

    # ------------------------------------------------------------ Drive

    def list_videos(self, folder_id: str) -> list[VideoItem]:
        """Video files directly inside the folder (no subfolders, no other file types)."""
        params: dict[str, Any] = {
            "q": f"'{folder_id}' in parents and trashed = false and mimeType contains 'video/'",
            "fields": "nextPageToken, files(id, name, mimeType, createdTime, size, webViewLink, "
                      "videoMediaMetadata(width, height, durationMillis))",
            "pageSize": 1000,
            "orderBy": "createdTime",
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
        }
        items: list[VideoItem] = []
        while True:
            data = self.request("GET", f"{DRIVE}/files", params=params).json()
            for f in data.get("files", []):
                items.append(_video_item(f))
            token = data.get("nextPageToken")
            if not token:
                break
            params["pageToken"] = token
        items.sort(key=lambda v: (v.created or datetime.max.astimezone(), _natural(v.name), v.id))
        return items

    def get_video(self, file_id: str) -> VideoItem:
        f = self.request("GET", f"{DRIVE}/files/{file_id}", params={
            "fields": "id, name, mimeType, createdTime, size, webViewLink, trashed, "
                      "videoMediaMetadata(width, height, durationMillis)",
            "supportsAllDrives": "true"}).json()
        return _video_item(f)

    def download(self, file_id: str, dest: Path, expected_size: int | None = None) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_suffix(dest.suffix + ".part")
        for attempt in range(1, self.attempts + 1):
            try:
                with self.session.get(f"{DRIVE}/files/{file_id}", params={"alt": "media", "supportsAllDrives": "true"},
                                      stream=True, timeout=(30, 300)) as resp:
                    if resp.status_code >= 400:
                        raise GoogleError(f"download HTTP {resp.status_code}: {_error_message(resp)}")
                    with part.open("wb") as fh:
                        for chunk in resp.iter_content(chunk_size=1 << 20):
                            fh.write(chunk)
                size = part.stat().st_size
                if expected_size is not None and size != expected_size:
                    raise GoogleError(f"download incomplete: {size} of {expected_size} bytes")
                part.replace(dest)
                return dest
            except (requests.RequestException, GoogleError) as exc:
                part.unlink(missing_ok=True)
                if attempt == self.attempts:
                    raise GoogleError(f"download of {file_id} failed: {exc}") from exc
                log.warning("download attempt %d failed (%s); retrying", attempt, exc)
                time.sleep(2 ** attempt)
        raise GoogleError("unreachable")

    def can_edit(self, file_id: str) -> bool:
        data = self.request("GET", f"{DRIVE}/files/{file_id}",
                            params={"fields": "capabilities(canEdit)", "supportsAllDrives": "true"}).json()
        return bool((data.get("capabilities") or {}).get("canEdit"))

    # ------------------------------------------------------------ Sheets

    def sheet_title(self, spreadsheet_id: str, index: int) -> str:
        data = self.request("GET", f"{SHEETS}/{spreadsheet_id}",
                            params={"fields": "sheets.properties(title,index)"}).json()
        sheets = sorted((s["properties"] for s in data.get("sheets", [])), key=lambda p: p["index"])
        if index >= len(sheets):
            raise GoogleError(f"spreadsheet has {len(sheets)} tab(s); worksheet_index {index} does not exist")
        return sheets[index]["title"]

    def read_range(self, spreadsheet_id: str, a1: str) -> list[list[str]]:
        data = self.request("GET", f"{SHEETS}/{spreadsheet_id}/values/{requests.utils.quote(a1, safe='')}",
                            params={"valueRenderOption": "FORMATTED_VALUE", "majorDimension": "ROWS"}).json()
        return data.get("values", [])

    def write_cell(self, spreadsheet_id: str, a1: str, value: str) -> None:
        self.request("PUT", f"{SHEETS}/{spreadsheet_id}/values/{requests.utils.quote(a1, safe='')}",
                     params={"valueInputOption": "RAW"}, json={"range": a1, "values": [[value]]})


def a1_sheet(title: str) -> str:
    return "'" + title.replace("'", "''") + "'"


def _video_item(f: dict) -> VideoItem:
    meta = f.get("videoMediaMetadata") or {}
    created = f.get("createdTime")
    return VideoItem(
        id=f["id"],
        name=f["name"],
        created=datetime.fromisoformat(created.replace("Z", "+00:00")) if created else None,
        size=int(f["size"]) if f.get("size") else None,
        width=meta.get("width"),
        height=meta.get("height"),
        duration_ms=int(meta["durationMillis"]) if meta.get("durationMillis") else None,
        web_link=f.get("webViewLink") or f"https://drive.google.com/file/d/{f['id']}/view",
    )


def _natural(name: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def _error_message(resp: requests.Response) -> str:
    try:
        return resp.json().get("error", {}).get("message", resp.text[:200])
    except ValueError:
        return resp.text[:200]
