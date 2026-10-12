"""Tests for environment-driven database configuration."""

import importlib

import backend.db.config as config


def _reload(monkeypatch, **env):
    keys = [
        "SERENEDB_HOST",
        "SERENEDB_PORT",
        "SERENEDB_DB",
        "SERENEDB_USER",
        "SERENEDB_PASSWORD",
        "SERENEDB_POOL_MIN",
        "SERENEDB_POOL_MAX",
        "SERENEDB_POOL_ACQUIRE_TIMEOUT",
        "SERENEDB_STATEMENT_TIMEOUT_MS",
    ]
    for key in keys:
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return importlib.reload(config)


def test_defaults(monkeypatch):
    cfg = _reload(monkeypatch)
    assert cfg.SERENEDB_HOST == "localhost"
    assert cfg.SERENEDB_PORT == 7890
    assert cfg.SERENEDB_DB == "postgres"
    assert cfg.SERENEDB_POOL_MIN == 2
    assert cfg.SERENEDB_POOL_MAX == 32
    assert cfg.SERENEDB_STATEMENT_TIMEOUT_MS == 30000


def test_overrides(monkeypatch):
    cfg = _reload(
        monkeypatch,
        SERENEDB_HOST="db.internal",
        SERENEDB_PORT="6000",
        SERENEDB_POOL_MAX="7",
        SERENEDB_POOL_ACQUIRE_TIMEOUT="1.5",
        SERENEDB_STATEMENT_TIMEOUT_MS="1234",
    )
    assert cfg.SERENEDB_HOST == "db.internal"
    assert cfg.SERENEDB_PORT == 6000
    assert cfg.SERENEDB_POOL_MAX == 7
    assert cfg.SERENEDB_POOL_ACQUIRE_TIMEOUT == 1.5
    assert cfg.SERENEDB_STATEMENT_TIMEOUT_MS == 1234
