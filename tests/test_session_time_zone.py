"""Every database session is pinned to UTC.

Offline: the pool and the standalone connection are replaced by fakes that
record the statements a cursor runs.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from sql_render import render  # noqa: E402

from backend.db import connection, probe  # noqa: E402


class _Cursor:
    def __init__(self, log: list[str]):
        self.log = log

    def execute(self, statement, params=None):
        self.log.append(statement if isinstance(statement, str) else render(statement))

    def close(self):
        pass


class _Conn:
    def __init__(self, log: list[str]):
        self.log = log
        self.autocommit = False

    def cursor(self, cursor_factory=None):
        return _Cursor(self.log)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


class _Pool:
    def __init__(self):
        self.borrowed: list[list[str]] = []

    def getconn(self):
        log: list[str] = []
        self.borrowed.append(log)
        return _Conn(log)

    def putconn(self, conn):
        pass


def test_every_pooled_borrow_sets_the_session_time_zone(monkeypatch):
    pool = _Pool()
    monkeypatch.setattr(connection, "_get_pool", lambda: pool)

    for _ in range(2):
        with connection.db_cursor() as cur:
            cur.execute("SELECT 1")

    assert len(pool.borrowed) == 2
    for log in pool.borrowed:
        # Issued before any caller statement on that borrow.
        assert log[0] == "SET TimeZone = 'UTC'"
        assert log[1] == "SELECT 1"


def test_the_probe_cursor_sets_the_session_time_zone(monkeypatch):
    log: list[str] = []
    monkeypatch.setattr(probe, "get_conn", lambda: _Conn(log))

    with probe.probe_cursor() as cur:
        cur.execute("SELECT 1")

    assert log[0] == "SET TimeZone = 'UTC'"
    assert "SELECT 1" in log


def test_the_session_zone_is_utc():
    assert connection.SESSION_TIME_ZONE == "UTC"
