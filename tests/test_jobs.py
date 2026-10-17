"""Tests for background investigations: the run registry and the job API.

Generation now outlives the request/script run that started it, so these cover
the properties that fix the UI bug: buffering, reconnection from a cursor,
idempotent starts, cancellation, and backend-owned persistence.
"""

import json
import threading
import time

from backend.jobs import CANCELLED, DONE, ERROR, RunRegistry


# --- Registry -----------------------------------------------------------------


def _wait_terminal(registry, run_id, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snapshot = registry.snapshot(run_id)
        if snapshot and snapshot["status"] in (DONE, ERROR, CANCELLED):
            return snapshot
        time.sleep(0.01)
    raise AssertionError("run did not reach a terminal state")


def _wait_events(registry, run_id, count, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snapshot = registry.snapshot(run_id)
        if snapshot and len(snapshot["events"]) >= count:
            return snapshot
        time.sleep(0.01)
    raise AssertionError("events did not arrive")


def _event(**payload) -> str:
    return json.dumps(payload) + "\n"


def test_registry_buffers_persists_and_derives_progress():
    registry = RunRegistry()
    finished = {}

    def produce(_run):
        yield _event(type="subgraph_name", content="normalize")
        yield _event(
            type="tool_progress",
            content=json.dumps({"operation": "count", "status": "completed"}),
        )
        yield _event(
            type="tool_results", content=json.dumps([{"id": "R1", "rows": [{"a": 1}]}])
        )
        yield _event(type="messages", content="Findings [[RESULT:R1]]")

    run = registry.start(
        user_id="u",
        chat_id="c",
        produce=produce,
        on_finish=lambda r: finished.update(status=r.status, result=r.result),
    )
    snapshot = _wait_terminal(registry, run.run_id)

    assert snapshot["status"] == DONE
    assert snapshot["result"]["answer"] == "Findings [[RESULT:R1]]"
    assert snapshot["result"]["tool_results"] == [{"id": "R1", "rows": [{"a": 1}]}]
    assert snapshot["result"]["progress"] == [
        "Currently at stage: normalize",
        "✓ count",
    ]
    assert [e["type"] for e in snapshot["events"]] == [
        "subgraph_name",
        "tool_progress",
        "tool_results",
        "messages",
    ]
    assert finished["status"] == DONE


def test_registry_mirrors_stage_event_as_current_stage():
    """The backend ``_track`` mirror maps a root-scope ``stage`` event to the
    same "Currently at stage" progress line the UI renders, matching the
    ``subgraph_name`` fixtures above."""
    registry = RunRegistry()

    def produce(_run):
        yield _event(type="stage", content="normalize")
        yield _event(type="messages", content="done")

    run = registry.start(
        user_id="u", chat_id="c", produce=produce, on_finish=lambda r: None
    )
    snapshot = _wait_terminal(registry, run.run_id)

    assert snapshot["result"]["progress"] == ["Currently at stage: normalize"]


def test_snapshot_replays_from_cursor():
    registry = RunRegistry()

    def produce(_run):
        yield _event(type="subgraph_name", content="a")
        yield _event(type="subgraph_name", content="b")
        yield _event(type="messages", content="done")

    run = registry.start(
        user_id="u", chat_id="c", produce=produce, on_finish=lambda r: None
    )
    _wait_terminal(registry, run.run_id)

    full = registry.snapshot(run.run_id, since=0)
    assert full["next_since"] == 3
    tail = registry.snapshot(run.run_id, since=1)
    assert [e["content"] for e in tail["events"]] == ["b", "done"]
    assert registry.snapshot(run.run_id, since=3)["events"] == []


def test_start_is_idempotent_per_chat_while_running():
    registry = RunRegistry()
    release = threading.Event()

    def produce(_run):
        release.wait(3)
        yield _event(type="messages", content="x")

    run1 = registry.start(
        user_id="u", chat_id="c", produce=produce, on_finish=lambda r: None
    )
    run2 = registry.start(
        user_id="u", chat_id="c", produce=produce, on_finish=lambda r: None
    )
    assert run1.run_id == run2.run_id
    assert registry.active_run("u", "c") == run1.run_id

    release.set()
    _wait_terminal(registry, run1.run_id)
    assert registry.active_run("u", "c") is None

    # A finished run frees the chat for a new one.
    run3 = registry.start(
        user_id="u", chat_id="c", produce=produce, on_finish=lambda r: None
    )
    assert run3.run_id != run1.run_id
    release.set()


def test_cancel_stops_before_late_events():
    registry = RunRegistry()
    finished = {}
    release = threading.Event()

    def produce(_run):
        yield _event(type="subgraph_name", content="start")
        release.wait(3)
        yield _event(type="messages", content="late")

    run = registry.start(
        user_id="u",
        chat_id="c",
        produce=produce,
        on_finish=lambda r: finished.update(status=r.status),
    )
    _wait_events(registry, run.run_id, 1)
    assert registry.cancel(run.run_id) is True
    release.set()

    snapshot = _wait_terminal(registry, run.run_id)
    assert snapshot["status"] == CANCELLED
    assert finished["status"] == CANCELLED
    # The late event arrived after cancellation and must not be buffered.
    assert len(snapshot["events"]) == 1


def test_failure_is_recorded_not_raised():
    registry = RunRegistry()
    finished = {}

    def produce(_run):
        raise RuntimeError("boom")
        yield  # pragma: no cover - makes this a generator function

    run = registry.start(
        user_id="u",
        chat_id="c",
        produce=produce,
        on_finish=lambda r: finished.update(status=r.status, error=r.error),
    )
    snapshot = _wait_terminal(registry, run.run_id)
    assert snapshot["status"] == ERROR
    assert "boom" in snapshot["error"]
    assert finished["status"] == ERROR


# --- API ----------------------------------------------------------------------


def _make_user(username: str) -> tuple[dict, dict]:
    """Create a user and session directly (independent of registration policy)."""
    from backend.appdb import auth

    user = auth.create_user(username, "password123")
    token, _ = auth.create_session(user["id"])
    return user, {"Authorization": f"Bearer {token}"}


def _poll_until_done(client, headers, chat_id, run_id, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(
            f"/chats/{chat_id}/run/{run_id}",
            params={"since": 0, "wait": 0.2},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        if payload["status"] != "running":
            return payload
    raise AssertionError("run did not finish")


def _create_chat(client, headers, chat_id="c1", role="engineer"):
    return client.post(
        "/chats", json={"chat_id": chat_id, "role": role}, headers=headers
    )


def test_investigate_persists_answer_without_the_client(client, api_module, monkeypatch):
    _user, headers = _make_user("investigator")
    _create_chat(client, headers)

    def fake_stream(_req, _user_id):
        yield _event(type="subgraph_name", content="normalize")
        yield _event(
            type="tool_results", content=json.dumps([{"id": "R1", "rows": [{"a": 1}]}])
        )
        yield _event(type="messages", content="Findings [[RESULT:R1]]")

    monkeypatch.setattr(api_module, "stream_events", fake_stream)

    response = client.post(
        "/chats/c1/investigate",
        json={
            "role": "engineer",
            "effort": "Medium",
            "question": "why?",
            "history": [{"role": "user", "content": "why?"}],
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text
    run_id = response.json()["run_id"]

    payload = _poll_until_done(client, headers, "c1", run_id)
    assert payload["status"] == DONE
    assert payload["result"]["answer"] == "Findings [[RESULT:R1]]"
    assert payload["result"]["tool_results"] == [{"id": "R1", "rows": [{"a": 1}]}]
    assert payload["result"]["progress"] == ["Currently at stage: normalize"]

    # The backend mirrors both turns into the store, so nothing is lost even
    # though no client stayed connected to the stream.
    messages = client.get("/chats/c1/messages", headers=headers).json()["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[0]["content"] == "why?"
    assert messages[1]["content"] == "Findings [[RESULT:R1]]"
    assert messages[1]["progress"] == ["Currently at stage: normalize"]


def test_investigate_is_idempotent_while_running(client, api_module, monkeypatch):
    _user, headers = _make_user("idempotent")
    _create_chat(client, headers)
    release = threading.Event()

    def slow_stream(_req, _user_id):
        yield _event(type="subgraph_name", content="start")
        release.wait(3)
        yield _event(type="messages", content="done")

    monkeypatch.setattr(api_module, "stream_events", slow_stream)
    body = {
        "role": "engineer",
        "effort": "Medium",
        "question": "q",
        "history": [{"role": "user", "content": "q"}],
    }

    first = client.post("/chats/c1/investigate", json=body, headers=headers).json()
    second = client.post("/chats/c1/investigate", json=body, headers=headers).json()
    assert first["run_id"] == second["run_id"]

    release.set()
    _poll_until_done(client, headers, "c1", first["run_id"])
    # A duplicate send must not persist a second user turn.
    messages = client.get("/chats/c1/messages", headers=headers).json()["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant"]


def test_active_run_reflects_inflight_investigation(client, api_module, monkeypatch):
    _user, headers = _make_user("reattacher")
    _create_chat(client, headers)
    release = threading.Event()

    def slow_stream(_req, _user_id):
        yield _event(type="subgraph_name", content="start")
        release.wait(3)
        yield _event(type="messages", content="done")

    monkeypatch.setattr(api_module, "stream_events", slow_stream)
    body = {
        "role": "engineer",
        "effort": "Medium",
        "question": "q",
        "history": [{"role": "user", "content": "q"}],
    }
    run_id = client.post(
        "/chats/c1/investigate", json=body, headers=headers
    ).json()["run_id"]

    active = client.get("/chats/c1/active-run", headers=headers).json()
    assert active["run_id"] == run_id

    release.set()
    _poll_until_done(client, headers, "c1", run_id)
    assert client.get("/chats/c1/active-run", headers=headers).json()["run_id"] is None


def test_delete_cancels_and_discards_inflight_run(client, api_module, monkeypatch):
    _user, headers = _make_user("deleter")
    _create_chat(client, headers)
    release = threading.Event()

    def slow_stream(_req, _user_id):
        yield _event(type="subgraph_name", content="start")
        release.wait(3)
        yield _event(type="messages", content="done")

    monkeypatch.setattr(api_module, "stream_events", slow_stream)
    body = {
        "role": "engineer",
        "effort": "Medium",
        "question": "q",
        "history": [{"role": "user", "content": "q"}],
    }
    run_id = client.post(
        "/chats/c1/investigate", json=body, headers=headers
    ).json()["run_id"]

    assert client.delete("/chats/c1", headers=headers).json()["deleted"] is True
    release.set()

    # The run is gone and the cancelled worker never persists an answer.
    assert client.get(f"/chats/c1/run/{run_id}", headers=headers).status_code == 404
    assert client.get("/chats/c1/messages", headers=headers).json()["messages"] == []


def test_run_endpoints_require_auth_and_ownership(client, api_module, monkeypatch):
    _owner, owner_headers = _make_user("owner-run")
    _other, other_headers = _make_user("other-run")
    _create_chat(client, owner_headers)

    release = threading.Event()

    def slow_stream(_req, _user_id):
        yield _event(type="subgraph_name", content="start")
        release.wait(3)
        yield _event(type="messages", content="done")

    monkeypatch.setattr(api_module, "stream_events", slow_stream)
    body = {
        "role": "engineer",
        "effort": "Medium",
        "question": "q",
        "history": [{"role": "user", "content": "q"}],
    }
    run_id = client.post(
        "/chats/c1/investigate", json=body, headers=owner_headers
    ).json()["run_id"]

    # Another user cannot read the run.
    assert (
        client.get(f"/chats/c1/run/{run_id}", headers=other_headers).status_code == 404
    )
    # A missing run is a 404, not a crash.
    assert (
        client.get("/chats/c1/run/does-not-exist", headers=owner_headers).status_code
        == 404
    )

    release.set()
    _poll_until_done(client, owner_headers, "c1", run_id)


# --- Answer latency -----------------------------------------------------------


def test_run_result_carries_the_elapsed_window():
    """The worker measures its own wall-clock window and reports it."""
    registry = RunRegistry()

    def produce(_run):
        time.sleep(0.05)
        yield _event(type="messages", content="hi")

    run = registry.start(
        user_id="u", chat_id="c", produce=produce, on_finish=lambda _r: None
    )
    _wait_terminal(registry, run.run_id)

    assert run.status == DONE
    assert run.elapsed_seconds >= 0.05
    assert run.result["elapsed_seconds"] == round(run.elapsed_seconds, 2)


def test_elapsed_window_is_reported_for_failed_runs_too():
    registry = RunRegistry()

    def produce(_run):
        yield _event(type="messages", content="partial")
        raise RuntimeError("boom")

    run = registry.start(
        user_id="u", chat_id="c", produce=produce, on_finish=lambda _r: None
    )
    _wait_terminal(registry, run.run_id)

    assert run.status == ERROR
    assert run.result["elapsed_seconds"] is not None


def test_poll_payload_exposes_elapsed_on_completion(client, api_module, monkeypatch):
    _user, headers = _make_user("latency")
    _create_chat(client, headers)

    def quick_stream(_req, _user_id):
        yield _event(type="messages", content="answer")

    monkeypatch.setattr(api_module, "stream_events", quick_stream)
    run_id = client.post(
        "/chats/c1/investigate",
        json={
            "role": "engineer",
            "effort": "Medium",
            "question": "q",
            "history": [{"role": "user", "content": "q"}],
        },
        headers=headers,
    ).json()["run_id"]

    payload = _poll_until_done(client, headers, "c1", run_id)
    assert payload["result"]["elapsed_seconds"] >= 0


# --- UI derivation ------------------------------------------------------------


def test_ui_derives_latency_from_stored_message_timestamps():
    from ui.main_page.main_page import _answer_elapsed, _format_duration

    history = [
        {"role": "user", "content": "q", "created_at": "2026-10-17T10:00:00+00:00"},
        {
            "role": "assistant",
            "content": "a",
            "created_at": "2026-10-17T10:01:03.5+00:00",
        },
    ]
    assert _answer_elapsed(history, 1, history[1]) == 63.5
    assert _format_duration(63.5) == "1m 03.5s"
    assert _format_duration(4.26) == "4.3s"

    # A live-finished run's own measurement wins over derivation.
    assert _answer_elapsed(history, 1, {"elapsed_seconds": 2.5}) == 2.5
    # Reframes and timestamp-less messages report nothing rather than a guess.
    assert (
        _answer_elapsed(
            history, 1, {"created_at": "2026-10-17T10:01:03.5+00:00", "reframed_for": "manager"}
        )
        is None
    )
    assert _answer_elapsed(history, 1, {"role": "assistant"}) is None
