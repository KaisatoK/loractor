"""Tests for the pooled connection layer (no live database required)."""

import time

import pytest
from psycopg2.pool import PoolError

from backend.db import connection


def test_connect_kwargs_include_timeout_and_app_name(monkeypatch):
    monkeypatch.setattr(connection, "SERENEDB_HOST", "dbhost")
    monkeypatch.setattr(connection, "SERENEDB_PORT", 1234)
    monkeypatch.setattr(connection, "SERENEDB_STATEMENT_TIMEOUT_MS", 4321)

    kwargs = connection._connect_kwargs()
    assert kwargs["host"] == "dbhost"
    assert kwargs["port"] == 1234
    assert kwargs["application_name"] == "ai-chatbot"
    assert "statement_timeout=4321" in kwargs["options"]


class _FakeCursor:
    def execute(self, *args, **kwargs):
        pass

    def close(self):
        pass


class _FakeConn:
    def __init__(self):
        self.closed = False
        self.committed = False
        self.rolled_back = False

    def cursor(self, cursor_factory=None):
        return _FakeCursor()

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True


class _FakePool:
    def __init__(self, conn=None):
        self._conn = conn or _FakeConn()
        self.returned = None

    def getconn(self):
        return self._conn

    def putconn(self, conn):
        self.returned = conn


def test_db_cursor_commits_and_returns_connection(monkeypatch):
    pool = _FakePool()
    monkeypatch.setattr(connection, "_get_pool", lambda: pool)

    with connection.db_cursor() as cur:
        assert isinstance(cur, _FakeCursor)

    assert pool.returned is pool._conn
    assert pool._conn.committed


def test_db_cursor_rolls_back_on_error(monkeypatch):
    pool = _FakePool()
    monkeypatch.setattr(connection, "_get_pool", lambda: pool)

    with pytest.raises(RuntimeError):
        with connection.db_cursor():
            raise RuntimeError("boom")

    assert pool._conn.rolled_back
    assert pool.returned is pool._conn  # connection still returned to the pool


def test_db_cursor_times_out_when_pool_exhausted(monkeypatch):
    class DeadPool:
        def getconn(self):
            raise PoolError("connection pool exhausted")

        def putconn(self, conn):
            pass

    monkeypatch.setattr(connection, "_get_pool", lambda: DeadPool())
    monkeypatch.setattr(connection, "SERENEDB_POOL_ACQUIRE_TIMEOUT", 0.1)

    start = time.monotonic()
    with pytest.raises(PoolError):
        with connection.db_cursor():
            pass
    assert time.monotonic() - start < 2.0
