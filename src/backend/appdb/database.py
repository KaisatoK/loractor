"""Application-data store (users, sessions, chats, messages).
Separate from the SereneDB log store. SQLite by default (durable, no extra
infra); a lock-guarded single connection suits the few-dozen concurrent users."""

import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

_DEFAULT_PATH = Path.cwd() / ".data" / "app.sqlite"

_lock = threading.RLock()
connection: sqlite3.Connection | None = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

CREATE TABLE IF NOT EXISTS chats (
    id         TEXT NOT NULL,
    user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role       TEXT NOT NULL,
    title      TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, id)
);

CREATE TABLE IF NOT EXISTS messages (
    user_id      TEXT NOT NULL,
    chat_id      TEXT NOT NULL,
    seq          INTEGER NOT NULL,
    role         TEXT NOT NULL,
    content      TEXT NOT NULL,
    tool_results TEXT,
    progress     TEXT,
    reframed_for TEXT,
    created_at   TEXT NOT NULL,
    PRIMARY KEY (user_id, chat_id, seq),
    FOREIGN KEY (user_id, chat_id) REFERENCES chats(user_id, id) ON DELETE CASCADE
);

-- Per-role answers for one assistant message (role switch). Each row is the
-- result of a separate investigation for that role, so switching back and
-- forth between roles reuses it instead of running again.
CREATE TABLE IF NOT EXISTS role_answers (
    user_id      TEXT NOT NULL,
    chat_id      TEXT NOT NULL,
    message_seq  INTEGER NOT NULL,
    role         TEXT NOT NULL,
    effort       TEXT NOT NULL,
    content      TEXT NOT NULL,
    tool_results TEXT,
    progress     TEXT,
    created_at   TEXT NOT NULL,
    PRIMARY KEY (user_id, chat_id, message_seq, role),
    FOREIGN KEY (user_id, chat_id) REFERENCES chats(user_id, id) ON DELETE CASCADE
);
"""


def db_path() -> Path:
    return Path(os.getenv("APP_DB_PATH", str(_DEFAULT_PATH)))


def _connect() -> sqlite3.Connection:
    global connection
    if connection is None:
        with _lock:
            if connection is None:
                path = db_path()
                path.parent.mkdir(parents=True, exist_ok=True)
                conn = sqlite3.connect(str(path), check_same_thread=False)
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA busy_timeout=5000")
                conn.execute("PRAGMA foreign_keys=ON")
                conn.executescript(_SCHEMA)
                conn.commit()
                connection = conn
    return connection


@contextmanager
def db():
    """Yield a cursor inside a lock; commit on success, rollback on error."""
    conn = _connect()
    with _lock:
        cur = conn.cursor()
        try:
            yield cur
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()
