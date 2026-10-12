"""Shared test fixtures.

Sets dummy provider keys and an in-memory checkpointer *before* the backend is
imported, and isolates each test on its own temporary app database.
"""

import os

os.environ.setdefault("GOOGLE_API_KEY", "test-key")
os.environ.setdefault("DEEPSEEK_API_KEY", "test-key")
os.environ.setdefault("GROQ_API_KEY", "test-key")
os.environ.setdefault("CHECKPOINT_BACKEND", "memory")

import pytest  # noqa: E402


@pytest.fixture()
def app_db(tmp_path, monkeypatch):
    """Point the app store at a fresh temp database and reset its connection."""
    import backend.appdb.database as database

    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "app.sqlite"))
    previous = database._conn
    database._conn = None
    yield
    if database._conn is not None:
        try:
            database._conn.close()
        except Exception:  # noqa: BLE001
            pass
    database._conn = previous


@pytest.fixture()
def api_module(app_db):
    """Import the API with a clean admission-control state."""
    import backend.api as api

    with api._slots_lock:
        api._user_slots.clear()
    yield api
    with api._slots_lock:
        api._user_slots.clear()


@pytest.fixture()
def client(api_module):
    from fastapi.testclient import TestClient

    with TestClient(api_module.app) as test_client:
        yield test_client


@pytest.fixture()
def registered(client):
    """A registered user: token, user record, and ready-to-use auth headers."""
    response = client.post(
        "/auth/register", json={"username": "alice", "password": "password123"}
    )
    assert response.status_code == 200, response.text
    data = response.json()
    return {
        "token": data["token"],
        "user": data["user"],
        "headers": {"Authorization": f"Bearer {data['token']}"},
    }
