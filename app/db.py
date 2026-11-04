"""SQLite: користувачі, сесії, закладки, кеш пошуку, журнал."""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager

from .config import settings

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY,
    username    TEXT NOT NULL UNIQUE COLLATE NOCASE,
    name        TEXT NOT NULL,
    pw_hash     TEXT NOT NULL,
    role        TEXT NOT NULL DEFAULT 'user',      -- user | admin
    status      TEXT NOT NULL DEFAULT 'pending',   -- pending | active | blocked
    created_at  INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    last_seen   INTEGER
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash  TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at  INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    expires_at  INTEGER NOT NULL,
    ua          TEXT
);
CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id);
CREATE TABLE IF NOT EXISTS bookmarks (
    id          INTEGER PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    cadnum      TEXT NOT NULL,
    note        TEXT NOT NULL DEFAULT '',
    lat         REAL, lng REAL,
    created_at  INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    UNIQUE(user_id, cadnum)
);
CREATE TABLE IF NOT EXISTS parcel_cache (
    cadnum      TEXT PRIMARY KEY,
    data        TEXT NOT NULL,
    fetched_at  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
    id          INTEGER PRIMARY KEY,
    ts          INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    user_id     INTEGER,
    action      TEXT NOT NULL,
    detail      TEXT
);
"""

_local = threading.local()


def conn() -> sqlite3.Connection:
    c = getattr(_local, "c", None)
    if c is None:
        c = sqlite3.connect(settings.db_path, timeout=10, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        _local.c = c
    return c


@contextmanager
def tx():
    c = conn()
    c.execute("BEGIN IMMEDIATE")
    try:
        yield c
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        raise


def init() -> None:
    conn().executescript(SCHEMA)


def audit(user_id: int | None, action: str, detail: str = "") -> None:
    conn().execute("INSERT INTO audit(user_id, action, detail) VALUES (?,?,?)",
                   (user_id, action, detail[:500]))
    # тримаємо журнал компактним
    conn().execute("DELETE FROM audit WHERE ts < strftime('%s','now') - 90*86400")
