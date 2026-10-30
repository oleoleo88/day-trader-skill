"""SQLite storage. One file, WAL mode, crash-safe work queue built on leases.

Every pipeline stage claims a row with a lease. If the process dies mid-step the lease
expires and the step is retried, so nothing is lost and nothing runs twice in parallel.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from .util import dumps, now

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);

CREATE TABLE IF NOT EXISTS sources_state (
  source_id TEXT PRIMARY KEY, etag TEXT, last_modified TEXT,
  last_poll REAL DEFAULT 0, last_ok REAL DEFAULT 0, last_error TEXT,
  failures INTEGER DEFAULT 0, disabled_until REAL DEFAULT 0, bootstrapped INTEGER DEFAULT 0,
  alpha REAL DEFAULT 1, beta REAL DEFAULT 1, items_total INTEGER DEFAULT 0);

CREATE TABLE IF NOT EXISTS dynamic_sources (
  id TEXT PRIMARY KEY, pack TEXT, spec TEXT, created_at REAL, active INTEGER DEFAULT 1);

CREATE TABLE IF NOT EXISTS items (
  id INTEGER PRIMARY KEY, source_id TEXT, pack TEXT, url TEXT UNIQUE, title TEXT, summary TEXT,
  content TEXT, image TEXT, lang TEXT, published_at REAL, fetched_at REAL, simhash TEXT,
  story_id INTEGER);
CREATE INDEX IF NOT EXISTS items_pack_time ON items(pack, fetched_at);
CREATE INDEX IF NOT EXISTS items_story ON items(story_id);

CREATE TABLE IF NOT EXISTS stories (
  id INTEGER PRIMARY KEY, pack TEXT, title TEXT, lang TEXT, simhash TEXT,
  first_seen REAL, last_seen REAL, item_count INTEGER DEFAULT 1, best_tier INTEGER);
CREATE INDEX IF NOT EXISTS stories_pack_time ON stories(pack, last_seen);

CREATE TABLE IF NOT EXISTS triage (
  channel_id TEXT, story_id INTEGER, score REAL, category TEXT, decision TEXT, reason TEXT,
  item_count INTEGER, ts REAL, PRIMARY KEY (channel_id, story_id));

CREATE TABLE IF NOT EXISTS posts (
  id INTEGER PRIMARY KEY, channel_id TEXT, story_id INTEGER, kind TEXT DEFAULT 'news',
  agent TEXT, status TEXT, score REAL DEFAULT 0, category TEXT, facts TEXT, draft TEXT,
  final TEXT, image TEXT, review TEXT, revisions INTEGER DEFAULT 0, confidence REAL,
  attempts INTEGER DEFAULT 0, lease_until REAL DEFAULT 0, last_error TEXT, checks INTEGER DEFAULT 0,
  created_at REAL, updated_at REAL, scheduled_at REAL, published_at REAL, expires_at REAL,
  message_id INTEGER, approval_msg_id INTEGER, owner_action TEXT, owner_note TEXT, sources TEXT);
CREATE INDEX IF NOT EXISTS posts_status ON posts(status, lease_until);
CREATE INDEX IF NOT EXISTS posts_channel_pub ON posts(channel_id, published_at);

CREATE TABLE IF NOT EXISTS metrics (
  post_id INTEGER, ts REAL, views INTEGER, reactions INTEGER, PRIMARY KEY (post_id, ts));

CREATE TABLE IF NOT EXISTS lessons (
  id INTEGER PRIMARY KEY, channel_id TEXT, role TEXT, text TEXT, origin TEXT,
  weight REAL DEFAULT 1, hits INTEGER DEFAULT 0, active INTEGER DEFAULT 1, created_at REAL);

CREATE TABLE IF NOT EXISTS agents (
  id INTEGER PRIMARY KEY, name TEXT UNIQUE, channel_id TEXT, spec TEXT, status TEXT,
  origin TEXT, created_at REAL, last_run REAL DEFAULT 0, runs INTEGER DEFAULT 0, notes TEXT);

CREATE TABLE IF NOT EXISTS proposals (
  id INTEGER PRIMARY KEY, kind TEXT, payload TEXT, rationale TEXT, status TEXT,
  created_at REAL, decided_at REAL, msg_id INTEGER);

CREATE TABLE IF NOT EXISTS llm_usage (
  ts REAL, role TEXT, provider TEXT, model TEXT, tin INTEGER, tout INTEGER, cost REAL,
  ok INTEGER, ms INTEGER, error TEXT);
CREATE INDEX IF NOT EXISTS llm_usage_ts ON llm_usage(ts);

CREATE TABLE IF NOT EXISTS mod_users (
  chat_id TEXT, user_id INTEGER, first_seen REAL, msgs INTEGER DEFAULT 0,
  strikes INTEGER DEFAULT 0, PRIMARY KEY (chat_id, user_id));

CREATE TABLE IF NOT EXISTS mod_actions (
  ts REAL, chat_id TEXT, user_id INTEGER, message_id INTEGER, action TEXT, reason TEXT, text TEXT);

CREATE TABLE IF NOT EXISTS events (ts REAL, level TEXT, component TEXT, msg TEXT);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
"""


class DB:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.conn.executescript(SCHEMA)
            self.conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    # ------------------------------------------------------------------ basics
    def x(self, sql: str, params=()) -> sqlite3.Cursor:
        with self.lock:
            return self.conn.execute(sql, params)

    def one(self, sql: str, params=()) -> sqlite3.Row | None:
        with self.lock:
            return self.conn.execute(sql, params).fetchone()

    def all(self, sql: str, params=()) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, params).fetchall()

    def scalar(self, sql: str, params=(), default=None):
        r = self.one(sql, params)
        return default if r is None or r[0] is None else r[0]

    def close(self):
        with self.lock:
            self.conn.close()

    # ------------------------------------------------------------------ kv
    def get(self, k: str, default=None):
        r = self.one("SELECT v FROM kv WHERE k=?", (k,))
        if r is None:
            return default
        try:
            return json.loads(r[0])
        except ValueError:
            return r[0]

    def set(self, k: str, v) -> None:
        self.x("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, dumps(v)))

    def delete(self, k: str) -> None:
        self.x("DELETE FROM kv WHERE k=?", (k,))

    # ------------------------------------------------------------------ events
    def event(self, level: str, component: str, msg: str) -> None:
        self.x("INSERT INTO events(ts,level,component,msg) VALUES(?,?,?,?)", (now(), level, component, msg[:2000]))

    # ------------------------------------------------------------------ posts queue
    def new_post(self, channel_id: str, story_id: int | None, status: str, **fields) -> int:
        t = now()
        cols = {"channel_id": channel_id, "story_id": story_id, "status": status,
                "created_at": t, "updated_at": t, **fields}
        for k, v in list(cols.items()):
            if isinstance(v, (dict, list)):
                cols[k] = dumps(v)
        keys = ",".join(cols)
        qs = ",".join("?" for _ in cols)
        return self.x(f"INSERT INTO posts({keys}) VALUES({qs})", tuple(cols.values())).lastrowid

    def claim(self, statuses: tuple[str, ...], lease_s: float = 300, channel_id: str | None = None,
              order: str = "score DESC, id") -> sqlite3.Row | None:
        """Atomically take one post in one of `statuses` whose lease has expired."""
        t = now()
        qs = ",".join("?" for _ in statuses)
        params: list = [t + lease_s, t, *statuses, t]
        extra = ""
        if channel_id:
            extra = " AND channel_id=?"
            params.append(channel_id)
        sql = (f"UPDATE posts SET lease_until=?, attempts=attempts+1, updated_at=? WHERE id=("
               f"SELECT id FROM posts WHERE status IN ({qs}) AND lease_until<?{extra} "
               f"ORDER BY {order} LIMIT 1) RETURNING *")
        with self.lock:
            return self.conn.execute(sql, params).fetchone()

    def advance(self, post_id: int, status: str, **fields) -> None:
        """Move a post to the next status and release its lease."""
        fields.update(status=status, lease_until=0, attempts=0, last_error=None, updated_at=now())
        self.update_post(post_id, **fields)

    def update_post(self, post_id: int, **fields) -> None:
        for k, v in list(fields.items()):
            if isinstance(v, (dict, list)):
                fields[k] = dumps(v)
        sets = ",".join(f"{k}=?" for k in fields)
        self.x(f"UPDATE posts SET {sets} WHERE id=?", (*fields.values(), post_id))

    def fail(self, post_id: int, error: str, max_attempts: int = 5) -> bool:
        """Record a failure; back off, or dead-letter after max_attempts. Returns True if dead."""
        r = self.one("SELECT attempts FROM posts WHERE id=?", (post_id,))
        attempts = r["attempts"] if r else max_attempts
        if attempts >= max_attempts:
            self.update_post(post_id, status="failed", last_error=error[:1000], lease_until=0, updated_at=now())
            return True
        delay = min(1800, 30 * 2 ** max(0, attempts - 1))
        self.update_post(post_id, last_error=error[:1000], lease_until=now() + delay, updated_at=now())
        return False

    def post(self, post_id: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM posts WHERE id=?", (post_id,))

    # ------------------------------------------------------------------ lessons
    def lessons(self, channel_id: str | None, role: str, k: int = 12) -> list[sqlite3.Row]:
        return self.all(
            "SELECT * FROM lessons WHERE active=1 AND role=? AND (channel_id IS NULL OR channel_id=?) "
            "ORDER BY weight DESC, created_at DESC LIMIT ?",
            (role, channel_id, k),
        )

    def add_lesson(self, channel_id: str | None, role: str, text: str, origin: str, weight: float = 1.0) -> int:
        return self.x(
            "INSERT INTO lessons(channel_id,role,text,origin,weight,created_at) VALUES(?,?,?,?,?,?)",
            (channel_id, role, text.strip()[:500], origin, weight, now()),
        ).lastrowid

    # ------------------------------------------------------------------ sources
    def source_state(self, sid: str) -> sqlite3.Row:
        r = self.one("SELECT * FROM sources_state WHERE source_id=?", (sid,))
        if r is None:
            self.x("INSERT OR IGNORE INTO sources_state(source_id) VALUES(?)", (sid,))
            r = self.one("SELECT * FROM sources_state WHERE source_id=?", (sid,))
        return r

    def update_source(self, sid: str, **fields) -> None:
        self.source_state(sid)
        sets = ",".join(f"{k}=?" for k in fields)
        self.x(f"UPDATE sources_state SET {sets} WHERE source_id=?", (*fields.values(), sid))

    def reliability(self, sid: str) -> tuple[float, float]:
        r = self.source_state(sid)
        return r["alpha"], r["beta"]

    # ------------------------------------------------------------------ llm spend
    def spend_since(self, ts: float) -> float:
        return float(self.scalar("SELECT SUM(cost) FROM llm_usage WHERE ts>=?", (ts,), 0.0))
