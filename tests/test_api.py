"""Integration tests for the HTTP API: auth gating, chats, streaming, limits."""

import json
import threading

import pytest
from fastapi import HTTPException

# --- Authentication gating ----------------------------------------------------


def test_unauthenticated_requests_are_rejected(client):
    assert client.get("/chats").status_code == 401
    assert client.get("/auth/me").status_code == 401
    assert client.get("/auto-complete", params={"query": "nova"}).status_code == 401
    assert (
        client.post(
            "/chat/stream",
            json={
                "chat_id": "c",
                "role": "engineer",
                "effort": "Medium",
                "history": [],
            },
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/chats/c/investigate",
            json={
                "role": "engineer",
                "effort": "Medium",
                "question": "q",
                "history": [{"role": "user", "content": "q"}],
            },
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/chat/reframe",
            json={
                "target_role": "manager",
                "effort": "Medium",
                "question": "q",
                "previous_answer": "a",
                "tool_results": [],
            },
        ).status_code
        == 401
    )


def test_auto_complete_returns_suggestions_for_authenticated_user(
    client, api_module, registered, monkeypatch
):
    # Stub the DB lookup: this exercises the route, not the query layer.
    monkeypatch.setattr(
        api_module,
        "auto_complete",
        lambda query: [{"suggestion": f"match:{query}", "count": 2}],
    )
    response = client.get(
        "/auto-complete", params={"query": "@nova"}, headers=registered["headers"]
    )
    assert response.status_code == 200
    assert response.json() == {
        "suggestions": [{"suggestion": "match:@nova", "count": 2}]
    }


def test_auth_flow_register_login_logout(client):
    registered = client.post(
        "/auth/register", json={"username": "alice", "password": "password123"}
    )
    assert registered.status_code == 200
    token = registered.json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    assert client.get("/auth/me", headers=headers).json()["username"] == "alice"
    assert (
        client.post(
            "/auth/login", json={"username": "alice", "password": "password123"}
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/auth/login", json={"username": "alice", "password": "wrongpass"}
        ).status_code
        == 401
    )
    assert client.post("/auth/logout", headers=headers).status_code == 200
    assert client.get("/auth/me", headers=headers).status_code == 401


def test_credential_validation(client):
    assert (
        client.post(
            "/auth/register", json={"username": "ab", "password": "password123"}
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/auth/register", json={"username": "validname", "password": "short"}
        ).status_code
        == 422
    )


def test_duplicate_username_conflicts(client):
    client.post("/auth/register", json={"username": "dup", "password": "password123"})
    assert (
        client.post(
            "/auth/register", json={"username": "dup", "password": "password123"}
        ).status_code
        == 409
    )


def test_registration_closed_returns_403(client, monkeypatch):
    from backend.appdb import auth

    monkeypatch.setattr(auth, "REGISTRATION_OPEN", False)
    response = client.post(
        "/auth/register", json={"username": "newuser", "password": "password123"}
    )
    assert response.status_code == 403


# --- Chats --------------------------------------------------------------------


def test_chat_crud_and_user_isolation(client, registered):
    headers = registered["headers"]
    assert (
        client.post(
            "/chats", json={"chat_id": "c1", "role": "engineer"}, headers=headers
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/chats/c1/messages",
            json={"role": "user", "content": "hi"},
            headers=headers,
        ).status_code
        == 200
    )
    messages = client.get("/chats/c1/messages", headers=headers).json()["messages"]
    assert [m["content"] for m in messages] == ["hi"]
    assert client.get("/chats", headers=headers).json()["chats"][0]["id"] == "c1"

    other = client.post(
        "/auth/register", json={"username": "bob", "password": "password123"}
    ).json()["token"]
    other_headers = {"Authorization": f"Bearer {other}"}
    assert client.get("/chats", headers=other_headers).json()["chats"] == []
    assert client.delete("/chats/c1", headers=other_headers).json()["deleted"] is False
    assert client.delete("/chats/c1", headers=headers).json()["deleted"] is True


def test_append_message_to_missing_chat_returns_404(client, registered):
    response = client.post(
        "/chats/nope/messages",
        json={"role": "user", "content": "x"},
        headers=registered["headers"],
    )
    assert response.status_code == 404


# --- Streaming ----------------------------------------------------------------


def test_chat_test_streams_ndjson(client, registered):
    response = client.post(
        "/chat/test",
        json={"chat_id": "c1", "role": "engineer", "effort": "Medium", "history": []},
        headers=registered["headers"],
    )
    assert response.status_code == 200
    lines = [line for line in response.text.splitlines() if line]
    events = [json.loads(line) for line in lines]
    assert events[0]["type"] == "subgraphs"
    assert any(event["type"] == "messages" for event in events)


def test_chat_stream_derives_user_from_token(
    client, api_module, registered, monkeypatch
):
    seen = {}

    class FakeRun:
        def interleave(self, *names):
            yield ("values", {"answer": "hello"})

    class FakeChatbot:
        def stream_events(self, state, config, **kwargs):
            seen["thread_id"] = config["configurable"]["thread_id"]
            return FakeRun()

    monkeypatch.setattr(api_module, "chatbot", FakeChatbot())

    response = client.post(
        "/chat/stream",
        json={
            "chat_id": "c1",
            "role": "engineer",
            "effort": "Medium",
            "history": [{"role": "user", "content": "hi"}],
        },
        headers=registered["headers"],
    )
    assert response.status_code == 200
    first = json.loads(response.text.splitlines()[0])
    assert first == {"type": "messages", "content": "hello"}
    # user id comes from the token, not the request body
    assert seen["thread_id"] == f"{registered['user']['id']}:c1"


# --- Admission control --------------------------------------------------------


def test_per_user_inflight_limit(api_module, registered):
    user_id = registered["user"]["id"]
    api_module.acquire_slot(user_id)
    api_module.acquire_slot(user_id)
    try:
        with pytest.raises(HTTPException) as excinfo:
            api_module.acquire_slot(user_id)
        assert excinfo.value.status_code == 429
    finally:
        api_module.release_slot(user_id)
        api_module.release_slot(user_id)


def test_global_inflight_limit(api_module, monkeypatch):
    monkeypatch.setattr(api_module, "global_slots", threading.BoundedSemaphore(1))
    with api_module.slots_lock:
        api_module.user_slots.clear()

    api_module.acquire_slot("user-a")
    try:
        with pytest.raises(HTTPException) as excinfo:
            api_module.acquire_slot("user-b")
        assert excinfo.value.status_code == 503
    finally:
        api_module.release_slot("user-a")


def test_release_is_idempotent_after_acquire(api_module):
    api_module.acquire_slot("u")
    api_module.release_slot("u")
    with api_module.slots_lock:
        assert "u" not in api_module.user_slots


# --- Operations ---------------------------------------------------------------


def test_health_and_metrics(client):
    assert client.get("/healthz").json()["status"] == "ok"
    metrics = client.get("/metrics").json()
    assert "counters" in metrics and "gauges" in metrics
    assert metrics["gauges"]["max_inflight_per_user"] >= 1


def test_readyz_reports_app_store(client):
    response = client.get("/readyz")
    assert response.json()["checks"]["app_store"] == "ok"
    assert response.status_code in (200, 503)  # 503 only if SereneDB is down


def test_request_id_is_generated_and_propagated(client):
    generated = client.get("/healthz").headers.get("x-request-id")
    assert generated
    echoed = client.get("/healthz", headers={"x-request-id": "trace-42"})
    assert echoed.headers["x-request-id"] == "trace-42"


def test_metrics_counters_increment(client, registered):
    before = client.get("/metrics").json()["counters"].get("auth_logins_ok", 0)
    client.post("/auth/login", json={"username": "alice", "password": "password123"})
    after = client.get("/metrics").json()["counters"].get("auth_logins_ok", 0)
    assert after == before + 1
