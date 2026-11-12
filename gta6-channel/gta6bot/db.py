"""SQLite-хранилище. Одно соединение на процесс, WAL, простые версии схемы."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Iterable

import aiosqlite

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source        TEXT NOT NULL,
    tier          INTEGER NOT NULL DEFAULT 2,
    lang          TEXT NOT NULL DEFAULT 'en',
    guid          TEXT NOT NULL,
    url           TEXT NOT NULL,
    url_canon     TEXT NOT NULL,
    title         TEXT NOT NULL,
    summary       TEXT NOT NULL DEFAULT '',
    content       TEXT NOT NULL DEFAULT '',
    image_url     TEXT,
    published_at  INTEGER NOT NULL,
    fetched_at    INTEGER NOT NULL,
    title_tokens  TEXT NOT NULL DEFAULT '',
    -- new | seeded | filtered | duplicate | rejected | thin | drafted | error
    status        TEXT NOT NULL DEFAULT 'new',
    dup_of        INTEGER,
    attempts      INTEGER NOT NULL DEFAULT 0,
    note          TEXT NOT NULL DEFAULT '',
    UNIQUE (source, guid)
);
CREATE INDEX IF NOT EXISTS items_status ON items (status, id);
CREATE INDEX IF NOT EXISTS items_canon ON items (url_canon);
CREATE INDEX IF NOT EXISTS items_fetched ON items (fetched_at);

CREATE TABLE IF NOT EXISTS posts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    -- news | breaking | digest | countdown | poll | manual | ad | suggestion
    kind            TEXT NOT NULL DEFAULT 'news',
    item_id         INTEGER,
    -- review | queued | publishing | published | rejected | expired | failed | deleted
    status          TEXT NOT NULL DEFAULT 'review',
    headline        TEXT NOT NULL DEFAULT '',
    text            TEXT NOT NULL DEFAULT '',
    image_url       TEXT,
    source_url      TEXT,
    source_name     TEXT,
    category        TEXT NOT NULL DEFAULT 'other',
    importance      INTEGER NOT NULL DEFAULT 5,
    confidence      REAL NOT NULL DEFAULT 0,
    created_at      INTEGER NOT NULL,
    not_before      INTEGER NOT NULL DEFAULT 0,
    published_at    INTEGER,
    channel_msg_id  INTEGER,
    discussion_msg_id INTEGER,
    review_msg_ids  TEXT NOT NULL DEFAULT '[]',
    delete_at       INTEGER,
    attempts        INTEGER NOT NULL DEFAULT 0,
    views           INTEGER,
    reactions       INTEGER,
    meta            TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS posts_status ON posts (status, not_before);
CREATE INDEX IF NOT EXISTS posts_msg ON posts (channel_msg_id);
CREATE INDEX IF NOT EXISTS posts_published ON posts (published_at);

CREATE TABLE IF NOT EXISTS mod_users (
    chat_id     INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    name        TEXT NOT NULL DEFAULT '',
    first_seen  INTEGER NOT NULL,
    msg_count   INTEGER NOT NULL DEFAULT 0,
    warns       INTEGER NOT NULL DEFAULT 0,
    last_warn_at INTEGER NOT NULL DEFAULT 0,
    trusted     INTEGER NOT NULL DEFAULT 0,
    banned      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (chat_id, user_id)
);

CREATE TABLE IF NOT EXISTS mod_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        INTEGER NOT NULL,
    chat_id   INTEGER NOT NULL,
    user_id   INTEGER NOT NULL,
    user_name TEXT NOT NULL DEFAULT '',
    action    TEXT NOT NULL,   -- delete | warn | mute | ban | unban | unmute
    rule      TEXT NOT NULL DEFAULT '',
    reason    TEXT NOT NULL DEFAULT '',
    text      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS mod_log_ts ON mod_log (ts);

CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS subs_daily (
    day         TEXT PRIMARY KEY,
    subscribers INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS joins (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        INTEGER NOT NULL,
    user_id   INTEGER NOT NULL,
    event     TEXT NOT NULL,      -- join | leave
    link      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS joins_ts ON joins (ts);
CREATE INDEX IF NOT EXISTS joins_user ON joins (user_id);

CREATE TABLE IF NOT EXISTS invite_links (
    link       TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    cost       REAL NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS cleanup (
    chat_id   INTEGER NOT NULL,
    msg_id    INTEGER NOT NULL,
    delete_at INTEGER NOT NULL,
    PRIMARY KEY (chat_id, msg_id)
);

CREATE TABLE IF NOT EXISTS relay (
    admin_msg_id INTEGER PRIMARY KEY,
    user_id      INTEGER NOT NULL,
    kind         TEXT NOT NULL DEFAULT 'dm',   -- dm | suggestion | ad
    ref_id       INTEGER NOT NULL DEFAULT 0,
    created_at   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS suggestions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    user_name  TEXT NOT NULL DEFAULT '',
    text       TEXT NOT NULL DEFAULT '',
    status     TEXT NOT NULL DEFAULT 'new'     -- new | accepted | rejected
);

CREATE TABLE IF NOT EXISTS llm_usage (
    day          TEXT NOT NULL,
    role         TEXT NOT NULL,
    model        TEXT NOT NULL,
    calls        INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read   INTEGER NOT NULL DEFAULT 0,
    cache_write  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, role, model)
);

CREATE TABLE IF NOT EXISTS source_health (
    source        TEXT PRIMARY KEY,
    ok_at         INTEGER NOT NULL DEFAULT 0,
    fail_at       INTEGER NOT NULL DEFAULT 0,
    fails         INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT NOT NULL DEFAULT '',
    etag          TEXT NOT NULL DEFAULT '',
    last_modified TEXT NOT NULL DEFAULT '',
    seeded        INTEGER NOT NULL DEFAULT 0,
    entries       INTEGER NOT NULL DEFAULT 0,
    next_poll_at  INTEGER NOT NULL DEFAULT 0
);
"""


def now() -> int:
    return int(time.time())


class Database:
    def __init__(self, conn: aiosqlite.Connection):
        self.conn = conn

    @classmethod
    async def open(cls, path: str | Path) -> "Database":
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(str(path))
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA synchronous=NORMAL")
        await conn.execute("PRAGMA busy_timeout=5000")
        await conn.executescript(SCHEMA)
        await conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        await conn.commit()
        return cls(conn)

    async def close(self) -> None:
        await self.conn.close()

    # ── базовые операции ────────────────────────────────────────────────────
    async def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        """Выполнить запрос, вернуть число затронутых строк."""
        cur = await self.conn.execute(sql, tuple(params))
        await self.conn.commit()
        return cur.rowcount

    async def insert(self, sql: str, params: Iterable[Any] = ()) -> int:
        cur = await self.conn.execute(sql, tuple(params))
        await self.conn.commit()
        return cur.lastrowid or 0

    async def one(self, sql: str, params: Iterable[Any] = ()) -> aiosqlite.Row | None:
        cur = await self.conn.execute(sql, tuple(params))
        return await cur.fetchone()

    async def all(self, sql: str, params: Iterable[Any] = ()) -> list[aiosqlite.Row]:
        cur = await self.conn.execute(sql, tuple(params))
        return list(await cur.fetchall())

    async def scalar(self, sql: str, params: Iterable[Any] = (), default: Any = None) -> Any:
        row = await self.one(sql, params)
        return default if row is None or row[0] is None else row[0]

    # ── kv ──────────────────────────────────────────────────────────────────
    async def kv_get(self, key: str, default: str | None = None) -> str | None:
        row = await self.one("SELECT v FROM kv WHERE k=?", (key,))
        return row["v"] if row else default

    async def kv_set(self, key: str, value: Any) -> None:
        await self.execute(
            "INSERT INTO kv (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (key, str(value)))

    async def kv_del(self, key: str) -> None:
        await self.execute("DELETE FROM kv WHERE k=?", (key,))

    async def kv_incr(self, key: str, by: int = 1) -> int:
        await self.execute(
            "INSERT INTO kv (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v=CAST(v AS INTEGER)+?",
            (key, str(by), by))
        return int(await self.kv_get(key, "0") or 0)

    # ── посты ───────────────────────────────────────────────────────────────
    async def post(self, post_id: int) -> aiosqlite.Row | None:
        return await self.one("SELECT * FROM posts WHERE id=?", (post_id,))

    async def create_post(self, **f: Any) -> int:
        f.setdefault("created_at", now())
        if isinstance(f.get("meta"), dict):
            f["meta"] = json.dumps(f["meta"], ensure_ascii=False)
        if isinstance(f.get("review_msg_ids"), list):
            f["review_msg_ids"] = json.dumps(f["review_msg_ids"])
        cols = ", ".join(f)
        marks = ", ".join("?" for _ in f)
        return await self.insert(f"INSERT INTO posts ({cols}) VALUES ({marks})", tuple(f.values()))

    async def update_post(self, post_id: int, **f: Any) -> None:
        if not f:
            return
        if isinstance(f.get("meta"), dict):
            f["meta"] = json.dumps(f["meta"], ensure_ascii=False)
        if isinstance(f.get("review_msg_ids"), list):
            f["review_msg_ids"] = json.dumps(f["review_msg_ids"])
        sets = ", ".join(f"{k}=?" for k in f)
        await self.execute(f"UPDATE posts SET {sets} WHERE id=?", (*f.values(), post_id))

    async def claim_post(self, post_id: int, from_statuses: tuple[str, ...]) -> bool:
        """Атомарно перевести пост в 'publishing'. False — его уже кто-то взял."""
        marks = ", ".join("?" for _ in from_statuses)
        n = await self.execute(
            f"UPDATE posts SET status='publishing' WHERE id=? AND status IN ({marks})", (post_id, *from_statuses))
        return n == 1

    # ── расписание удаления служебных сообщений ─────────────────────────────
    async def schedule_delete(self, chat_id: int, msg_id: int, in_seconds: int) -> None:
        await self.execute(
            "INSERT OR REPLACE INTO cleanup (chat_id, msg_id, delete_at) VALUES (?, ?, ?)",
            (chat_id, msg_id, now() + in_seconds))


def meta_of(row: Any) -> dict:
    try:
        return json.loads(row["meta"] or "{}")
    except (TypeError, ValueError, KeyError, IndexError):
        return {}
