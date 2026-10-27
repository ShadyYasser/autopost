"""SQLite state (committed back to the repo after each slot).

All timestamps are stored as UTC ISO-8601 strings with seconds precision so they
sort and compare correctly as text.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS daily_slots (
    brand TEXT NOT NULL,
    slot_date TEXT NOT NULL,
    slot TEXT NOT NULL,
    target_utc TEXT NOT NULL,
    deadline_utc TEXT,
    window TEXT,
    status TEXT NOT NULL DEFAULT 'scheduled',   -- scheduled|in_progress|done|partial|skipped
    note TEXT,
    created_utc TEXT NOT NULL,
    updated_utc TEXT NOT NULL,
    PRIMARY KEY (brand, slot_date, slot)
);

CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    brand TEXT NOT NULL,
    stream TEXT NOT NULL,
    slot_date TEXT NOT NULL,
    slot TEXT NOT NULL,
    source_id TEXT NOT NULL,          -- Drive file id
    source_name TEXT NOT NULL,
    source_created_utc TEXT,
    text_row INTEGER,                 -- sheet row used for the text (sheet sources)
    variables_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'in_progress', -- in_progress|done|partial|failed
    created_utc TEXT NOT NULL,
    updated_utc TEXT NOT NULL,
    UNIQUE (brand, stream, slot_date, slot)
);
CREATE INDEX IF NOT EXISTS posts_source ON posts (brand, stream, source_id);

CREATE TABLE IF NOT EXISTS deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id INTEGER NOT NULL REFERENCES posts(id),
    platform TEXT NOT NULL,
    provider TEXT NOT NULL,
    position INTEGER NOT NULL,        -- shuffled publishing order inside the post
    status TEXT NOT NULL DEFAULT 'pending',
        -- pending|checking|sent|manual|failed|gave_up|skipped_deadline
    attempts INTEGER NOT NULL DEFAULT 0,
    external_id TEXT,
    url TEXT,
    last_error TEXT,
    check_after_utc TEXT,
    updated_utc TEXT NOT NULL,
    UNIQUE (post_id, platform)
);

CREATE TABLE IF NOT EXISTS caption_cursor (
    brand TEXT PRIMARY KEY,
    last_row INTEGER NOT NULL,
    updated_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS buffer_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account TEXT NOT NULL,
    ts_utc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS buffer_requests_acct ON buffer_requests (account, ts_utc);

CREATE TABLE IF NOT EXISTS alerts_sent (
    key TEXT PRIMARY KEY,
    sent_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);

-- Deliveries made outside the schedule (test-post). When the video's normal turn comes,
-- these platforms count as already done and the same texts are reused.
CREATE TABLE IF NOT EXISTS early_deliveries (
    brand TEXT NOT NULL,
    stream TEXT NOT NULL,
    source_id TEXT NOT NULL,
    platform TEXT NOT NULL,
    provider TEXT NOT NULL,
    status TEXT NOT NULL,             -- sent | manual
    external_id TEXT,
    url TEXT,
    variables_json TEXT NOT NULL,
    delivered_utc TEXT NOT NULL,
    PRIMARY KEY (brand, stream, source_id, platform)
);
"""

FINAL_DELIVERY = {"sent", "manual", "gave_up", "skipped_deadline"}


def to_utc_str(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise ValueError("naive datetime")
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def from_utc_str(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


@dataclass
class SlotRow:
    brand: str
    slot_date: date
    slot: str
    target: datetime
    deadline: datetime | None
    window: str | None
    status: str
    note: str | None


@dataclass
class DeliveryRow:
    id: int
    post_id: int
    platform: str
    provider: str
    position: int
    status: str
    attempts: int
    external_id: str | None
    url: str | None
    last_error: str | None
    check_after: datetime | None


class State:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(SCHEMA)
        row = self.db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if row is None:
            self.db.execute("INSERT INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
        elif int(row["value"]) > SCHEMA_VERSION:
            raise RuntimeError(f"state.db schema {row['value']} is newer than this code ({SCHEMA_VERSION})")

    def close(self) -> None:
        self.db.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self.db.execute("BEGIN")
        try:
            yield
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        self.db.execute("COMMIT")

    def _exec(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        return self.db.execute(sql, tuple(params))

    # ------------------------------------------------------------ slots

    def has_slots(self, brand: str, day: date) -> bool:
        return self._exec(
            "SELECT 1 FROM daily_slots WHERE brand=? AND slot_date=? LIMIT 1", (brand, day.isoformat())
        ).fetchone() is not None

    def insert_slot(self, brand: str, day: date, slot: str, target: datetime, deadline: datetime | None,
                    window: str | None, now: datetime, status: str = "scheduled", note: str | None = None) -> None:
        n = to_utc_str(now)
        self._exec(
            "INSERT OR IGNORE INTO daily_slots VALUES (?,?,?,?,?,?,?,?,?,?)",
            (brand, day.isoformat(), slot, to_utc_str(target),
             to_utc_str(deadline) if deadline else None, window, status, note, n, n),
        )

    def slots(self, brand: str | None = None, days: Iterable[date] | None = None,
              statuses: Iterable[str] | None = None) -> list[SlotRow]:
        sql, params = "SELECT * FROM daily_slots WHERE 1=1", []
        if brand:
            sql += " AND brand=?"
            params.append(brand)
        if days is not None:
            days = [d.isoformat() for d in days]
            sql += f" AND slot_date IN ({','.join('?' * len(days))})"
            params += days
        if statuses is not None:
            statuses = list(statuses)
            sql += f" AND status IN ({','.join('?' * len(statuses))})"
            params += statuses
        sql += " ORDER BY target_utc"
        return [
            SlotRow(r["brand"], date.fromisoformat(r["slot_date"]), r["slot"], from_utc_str(r["target_utc"]),
                    from_utc_str(r["deadline_utc"]), r["window"], r["status"], r["note"])
            for r in self._exec(sql, params)
        ]

    def set_slot_status(self, brand: str, day: date, slot: str, status: str, now: datetime,
                        note: str | None = None) -> None:
        self._exec(
            "UPDATE daily_slots SET status=?, note=COALESCE(?, note), updated_utc=? "
            "WHERE brand=? AND slot_date=? AND slot=?",
            (status, note, to_utc_str(now), brand, day.isoformat(), slot),
        )

    # ------------------------------------------------------------ posts / deliveries

    def used_source_ids(self, brand: str, stream: str) -> set[str]:
        return {r[0] for r in self._exec(
            "SELECT source_id FROM posts WHERE brand=? AND stream=?", (brand, stream))}

    def create_post(self, brand: str, stream: str, day: date, slot: str, source_id: str, source_name: str,
                    source_created: datetime | None, text_row: int | None, variables: dict[str, str],
                    platforms: list[tuple[str, str]], now: datetime) -> int:
        """Create a post and its deliveries atomically. `platforms` is [(platform, provider)] in publish order."""
        n = to_utc_str(now)
        with self.transaction():
            cur = self._exec(
                "INSERT INTO posts (brand, stream, slot_date, slot, source_id, source_name, source_created_utc,"
                " text_row, variables_json, status, created_utc, updated_utc)"
                " VALUES (?,?,?,?,?,?,?,?,?,'in_progress',?,?)",
                (brand, stream, day.isoformat(), slot, source_id, source_name,
                 to_utc_str(source_created) if source_created else None, text_row,
                 json.dumps(variables, ensure_ascii=False), n, n),
            )
            post_id = cur.lastrowid
            for pos, (platform, provider) in enumerate(platforms):
                self._exec(
                    "INSERT INTO deliveries (post_id, platform, provider, position, status, updated_utc)"
                    " VALUES (?,?,?,?, 'pending', ?)",
                    (post_id, platform, provider, pos, n),
                )
        return post_id

    def post(self, post_id: int) -> sqlite3.Row:
        return self._exec("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()

    def posts_for_slot(self, brand: str, day: date, slot: str) -> list[sqlite3.Row]:
        return list(self._exec("SELECT * FROM posts WHERE brand=? AND slot_date=? AND slot=? ORDER BY id",
                               (brand, day.isoformat(), slot)))

    def recent_posts(self, limit: int = 20) -> list[sqlite3.Row]:
        return list(self._exec("SELECT * FROM posts ORDER BY id DESC LIMIT ?", (limit,)))

    def set_post_status(self, post_id: int, status: str, now: datetime) -> None:
        self._exec("UPDATE posts SET status=?, updated_utc=? WHERE id=?", (status, to_utc_str(now), post_id))

    def deliveries(self, post_id: int) -> list[DeliveryRow]:
        return [self._delivery(r) for r in self._exec(
            "SELECT * FROM deliveries WHERE post_id=? ORDER BY position", (post_id,))]

    def delivery(self, delivery_id: int) -> DeliveryRow:
        return self._delivery(self._exec("SELECT * FROM deliveries WHERE id=?", (delivery_id,)).fetchone())

    @staticmethod
    def _delivery(r: sqlite3.Row) -> DeliveryRow:
        return DeliveryRow(r["id"], r["post_id"], r["platform"], r["provider"], r["position"], r["status"],
                           r["attempts"], r["external_id"], r["url"], r["last_error"],
                           from_utc_str(r["check_after_utc"]))

    def update_delivery(self, delivery_id: int, now: datetime, *, status: str | None = None,
                        attempts_inc: int = 0, external_id: str | None = None, url: str | None = None,
                        error: str | None = None, provider: str | None = None,
                        check_after: datetime | None = None) -> None:
        self._exec(
            "UPDATE deliveries SET status=COALESCE(?, status), attempts=attempts+?,"
            " external_id=COALESCE(?, external_id), url=COALESCE(?, url), last_error=COALESCE(?, last_error),"
            " provider=COALESCE(?, provider), check_after_utc=?, updated_utc=? WHERE id=?",
            (status, attempts_inc, external_id, url, error, provider,
             to_utc_str(check_after) if check_after else None, to_utc_str(now), delivery_id),
        )

    # ------------------------------------------------------------ early (test) deliveries

    def record_early_delivery(self, brand: str, stream: str, source_id: str, platform: str, provider: str,
                              status: str, external_id: str | None, url: str | None, variables: dict[str, str],
                              now: datetime) -> None:
        self._exec("INSERT OR REPLACE INTO early_deliveries VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (brand, stream, source_id, platform, provider, status, external_id, url,
                    json.dumps(variables, ensure_ascii=False), to_utc_str(now)))

    def early_deliveries(self, brand: str, stream: str, source_id: str) -> list[sqlite3.Row]:
        return list(self._exec("SELECT * FROM early_deliveries WHERE brand=? AND stream=? AND source_id=? "
                               "ORDER BY delivered_utc", (brand, stream, source_id)))

    # ------------------------------------------------------------ caption cursor

    def caption_cursor(self, brand: str) -> tuple[int, datetime] | None:
        """(last sheet row used, when) or None."""
        r = self._exec("SELECT last_row, updated_utc FROM caption_cursor WHERE brand=?", (brand,)).fetchone()
        return None if r is None else (r[0], from_utc_str(r[1]))

    def set_caption_cursor(self, brand: str, row: int, now: datetime) -> None:
        self._exec("INSERT INTO caption_cursor VALUES (?,?,?) ON CONFLICT(brand) DO UPDATE SET "
                   "last_row=excluded.last_row, updated_utc=excluded.updated_utc", (brand, row, to_utc_str(now)))

    # ------------------------------------------------------------ buffer usage

    def record_buffer_request(self, account: str, now: datetime) -> None:
        self._exec("INSERT INTO buffer_requests (account, ts_utc) VALUES (?,?)", (account, to_utc_str(now)))

    def buffer_requests_since(self, account: str, since: datetime) -> int:
        return self._exec("SELECT COUNT(*) FROM buffer_requests WHERE account=? AND ts_utc>=?",
                          (account, to_utc_str(since))).fetchone()[0]

    def prune(self, now: datetime) -> None:
        self._exec("DELETE FROM buffer_requests WHERE ts_utc < ?", (to_utc_str(now - timedelta(days=31)),))
        self._exec("DELETE FROM alerts_sent WHERE sent_utc < ?", (to_utc_str(now - timedelta(days=60)),))

    # ------------------------------------------------------------ alerts / kv

    def alert_sent(self, key: str) -> bool:
        return self._exec("SELECT 1 FROM alerts_sent WHERE key=?", (key,)).fetchone() is not None

    def mark_alert(self, key: str, now: datetime) -> None:
        self._exec("INSERT OR REPLACE INTO alerts_sent VALUES (?,?)", (key, to_utc_str(now)))

    def kv_get(self, key: str) -> str | None:
        r = self._exec("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return None if r is None else r[0]

    def kv_set(self, key: str, value: str) -> None:
        self._exec("INSERT OR REPLACE INTO kv VALUES (?,?)", (key, value))
