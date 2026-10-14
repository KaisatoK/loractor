"""Tests for checkpointer backend selection."""

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from backend.pipeline.graph import build_checkpointer


def test_memory_backend(monkeypatch):
    monkeypatch.setenv("CHECKPOINT_BACKEND", "memory")
    assert isinstance(build_checkpointer(), InMemorySaver)


def test_sqlite_backend_creates_file(monkeypatch, tmp_path):
    from langgraph.checkpoint.sqlite import SqliteSaver

    path = tmp_path / "checkpoints.sqlite"
    monkeypatch.setenv("CHECKPOINT_BACKEND", "sqlite")
    monkeypatch.setenv("CHECKPOINT_SQLITE_PATH", str(path))

    saver = build_checkpointer()
    assert isinstance(saver, SqliteSaver)
    assert path.exists()


def test_unknown_backend_raises(monkeypatch):
    monkeypatch.setenv("CHECKPOINT_BACKEND", "bogus")
    with pytest.raises(ValueError):
        build_checkpointer()


def test_postgres_backend_requires_url(monkeypatch):
    monkeypatch.setenv("CHECKPOINT_BACKEND", "postgres")
    monkeypatch.delenv("CHECKPOINT_POSTGRES_URL", raising=False)
    with pytest.raises(RuntimeError):
        build_checkpointer()
