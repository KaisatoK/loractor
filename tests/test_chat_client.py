"""Tests for the UI backend HTTP client (requests mocked)."""

import json

import pytest
import requests

from ui import chat_client


class FakeResponse:
    def __init__(self, json_data=None, lines=None, status=200):
        self._json = json_data
        self._lines = lines or []
        self.status_code = status
        self.headers = {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(response=self)

    def json(self):
        return self._json

    def iter_lines(self, decode_unicode=True):
        return iter(self._lines)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_login_posts_credentials(monkeypatch):
    captured = {}

    def fake_post(url, json=None, timeout=None, headers=None):
        captured.update(url=url, payload=json)
        return FakeResponse({"token": "t", "user": {"id": "1", "username": "u"}})

    monkeypatch.setattr(chat_client.requests, "post", fake_post)
    result = chat_client.login("u", "p")
    assert result["token"] == "t"
    assert captured["url"].endswith("/auth/login")
    assert captured["payload"] == {"username": "u", "password": "p"}


def test_stream_chat_parses_ndjson_and_sends_bearer(monkeypatch):
    lines = [
        json.dumps({"type": "messages", "content": "hi"}),
        "",
        json.dumps({"type": "tool_results", "content": "[]"}),
    ]
    captured = {}

    def fake_post(url, json=None, headers=None, stream=None, timeout=None):
        captured["headers"] = headers
        captured["payload"] = json
        return FakeResponse(lines=lines)

    monkeypatch.setattr(chat_client.requests, "post", fake_post)
    events = list(
        chat_client.stream_chat(
            token="tok", chat_id="c1", role="engineer", effort="Medium", history=[]
        )
    )
    assert events == [
        {"type": "messages", "content": "hi"},
        {"type": "tool_results", "content": "[]"},
    ]
    assert captured["headers"]["Authorization"] == "Bearer tok"
    assert captured["payload"]["chat_id"] == "c1"


def test_reframe_returns_answer(monkeypatch):
    monkeypatch.setattr(
        chat_client.requests, "post", lambda *a, **k: FakeResponse({"answer": "A"})
    )
    assert (
        chat_client.reframe(
            token="t",
            target_role="manager",
            effort="Medium",
            question="q",
            previous_answer="p",
            tool_results=[],
        )
        == "A"
    )


def test_list_chats_sends_bearer(monkeypatch):
    captured = {}

    def fake_get(url, headers=None, timeout=None):
        captured["headers"] = headers
        return FakeResponse({"chats": [{"id": "c1"}]})

    monkeypatch.setattr(chat_client.requests, "get", fake_get)
    assert chat_client.list_chats("tok") == [{"id": "c1"}]
    assert captured["headers"] == {"Authorization": "Bearer tok"}


def test_append_message_payload(monkeypatch):
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured.update(url=url, payload=json)
        return FakeResponse({})

    monkeypatch.setattr(chat_client.requests, "post", fake_post)
    chat_client.append_message(
        "t",
        "c1",
        role="assistant",
        content="yo",
        tool_results=[{"id": "R1"}],
        progress=["stage"],
        reframed_for="manager",
    )
    assert captured["url"].endswith("/chats/c1/messages")
    assert captured["payload"] == {
        "role": "assistant",
        "content": "yo",
        "tool_results": [{"id": "R1"}],
        "progress": ["stage"],
        "reframed_for": "manager",
    }


def test_http_error_is_raised(monkeypatch):
    monkeypatch.setattr(
        chat_client.requests, "get", lambda *a, **k: FakeResponse(status=401)
    )
    with pytest.raises(requests.HTTPError):
        chat_client.list_chats("bad-token")


def test_start_investigation_payload(monkeypatch):
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured.update(url=url, payload=json, headers=headers)
        return FakeResponse({"run_id": "r1", "status": "running"})

    monkeypatch.setattr(chat_client.requests, "post", fake_post)
    result = chat_client.start_investigation(
        token="t",
        chat_id="c1",
        role="engineer",
        effort="Medium",
        question="q",
        history=[{"role": "user", "content": "q"}],
    )
    assert result == {"run_id": "r1", "status": "running"}
    assert captured["url"].endswith("/chats/c1/investigate")
    assert captured["payload"]["question"] == "q"
    assert captured["headers"] == {"Authorization": "Bearer t"}


def test_get_run_sends_cursor_and_wait(monkeypatch):
    captured = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        captured.update(url=url, params=params, headers=headers)
        return FakeResponse(
            {
                "status": "running",
                "events": [],
                "next_since": 0,
                "result": None,
                "error": None,
            }
        )

    monkeypatch.setattr(chat_client.requests, "get", fake_get)
    chat_client.get_run(token="t", chat_id="c1", run_id="r1", since=3, wait=2.0)
    assert captured["url"].endswith("/chats/c1/run/r1")
    assert captured["params"] == {"since": 3, "wait": 2.0}


def test_active_run_returns_id(monkeypatch):
    monkeypatch.setattr(
        chat_client.requests, "get", lambda *a, **k: FakeResponse({"run_id": "r9"})
    )
    assert chat_client.active_run(token="t", chat_id="c1") == "r9"


def test_cancel_run_posts(monkeypatch):
    captured = {}

    def fake_post(url, headers=None, timeout=None):
        captured["url"] = url
        return FakeResponse({"cancelled": True})

    monkeypatch.setattr(chat_client.requests, "post", fake_post)
    chat_client.cancel_run(token="t", chat_id="c1", run_id="r1")
    assert captured["url"].endswith("/chats/c1/run/r1/cancel")

