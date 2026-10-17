"""Role switch: a new per-role investigation, cached per (message, role).

Offline: ``stream_events`` is replaced by a fake that records how it was called,
so these check the endpoint contract, the cache, slot isolation from the chat's
main run, and cleanup on delete without any model or database.
"""

import json
import threading
import time

from backend.appdb import auth, chats
from backend.jobs import DONE


def _event(**payload) -> str:
    return json.dumps(payload) + "\n"


def _user(username):
    user = auth.create_user(username, "password123")
    token, _ = auth.create_session(user["id"])
    return user, {"Authorization": f"Bearer {token}"}


def _seed_chat(user_id, chat_id="c1"):
    chats.create_chat(user_id, chat_id, "engineer")
    chats.append_message(user_id, chat_id, "user", "Why do spawns fail?")
    chats.append_message(
        user_id, chat_id, "assistant", "Engineer answer", tool_results=[{"id": "R1"}]
    )


def _poll(client, headers, chat_id, run_id, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        payload = client.get(
            f"/chats/{chat_id}/run/{run_id}", params={"wait": 0.2}, headers=headers
        ).json()
        if payload["status"] != "running":
            return payload
    raise AssertionError("run did not finish")


def _fake_stream(calls, answer="Manager answer"):
    def fake(req, user_id, *, thread_id=None, extra_state=None):
        calls.append({"req": req, "thread_id": thread_id, "extra_state": extra_state})
        yield _event(type="tool_results", content=json.dumps([{"id": "R1", "kind": "aggregate"}]))
        yield _event(type="messages", content=answer)

    return fake


def test_messages_expose_seq(app_db):
    user = auth.create_user("seq-user", "password123")
    _seed_chat(user["id"])
    assert [m["seq"] for m in chats.get_messages(user["id"], "c1")] == [1, 2]


def test_role_switch_runs_new_investigation_then_serves_cache(client, api_module, monkeypatch):
    user, headers = _user("switcher")
    _seed_chat(user["id"])
    calls = []
    monkeypatch.setattr(api_module, "stream_events", _fake_stream(calls))

    first = client.post(
        "/chats/c1/messages/2/role-answer",
        json={"role": "manager", "effort": "Medium"},
        headers=headers,
    ).json()
    assert first["status"] in ("running", "done") and first["run_id"]
    assert _poll(client, headers, "c1", first["run_id"])["status"] == DONE

    # It was a fresh investigation for the target role, on its own thread,
    # with the question (not the other role's answer) as history.
    (call,) = calls
    assert call["req"]["role"] == "manager"
    assert call["req"]["history"] == [{"role": "user", "content": "Why do spawns fail?"}]
    assert call["extra_state"] == {"forced_scope": "find_evidence"}
    assert call["thread_id"].endswith(":role:2:manager:Medium")

    # Second request at the same (or lower) effort is a cache hit: no new run.
    for effort in ("Medium", "Low"):
        again = client.post(
            "/chats/c1/messages/2/role-answer",
            json={"role": "manager", "effort": effort},
            headers=headers,
        ).json()
        assert again["status"] == "cached"
        assert again["answer"]["content"] == "Manager answer"
        assert again["answer"]["tool_results"] == [{"id": "R1", "kind": "aggregate"}]
    assert len(calls) == 1

    listed = client.get("/chats/c1/role-answers", headers=headers).json()["answers"]
    assert [(a["message_seq"], a["role"], a["effort"]) for a in listed] == [(2, "manager", "Medium")]
    # The chat's own history is untouched by a role switch.
    assert [m["role"] for m in chats.get_messages(user["id"], "c1")] == ["user", "assistant"]


def test_higher_effort_reruns_and_replaces_cache(client, api_module, monkeypatch):
    user, headers = _user("deeper")
    _seed_chat(user["id"])
    chats.save_role_answer(user["id"], "c1", 2, "team_lead", "Low", "shallow")
    calls = []
    monkeypatch.setattr(api_module, "stream_events", _fake_stream(calls, answer="deep"))

    started = client.post(
        "/chats/c1/messages/2/role-answer",
        json={"role": "team_lead", "effort": "High"},
        headers=headers,
    ).json()
    assert started["status"] in ("running", "done") and started["run_id"]
    _poll(client, headers, "c1", started["run_id"])
    cached = chats.get_role_answer(user["id"], "c1", 2, "team_lead")
    assert (cached["effort"], cached["content"]) == ("High", "deep")


def test_role_switch_slot_does_not_block_main_run(client, api_module, monkeypatch):
    user, headers = _user("parallel")
    _seed_chat(user["id"])
    release = threading.Event()

    def slow(req, user_id, *, thread_id=None, extra_state=None):
        yield _event(type="stage", content="normalize")
        release.wait(3)
        yield _event(type="messages", content="done")

    monkeypatch.setattr(api_module, "stream_events", slow)
    role_run = client.post(
        "/chats/c1/messages/2/role-answer", json={"role": "manager"}, headers=headers
    ).json()
    # The chat's main slot is still free.
    assert client.get("/chats/c1/active-run", headers=headers).json()["run_id"] is None
    # Requesting the same switch again joins the in-flight run.
    again = client.post(
        "/chats/c1/messages/2/role-answer", json={"role": "manager"}, headers=headers
    ).json()
    assert again["run_id"] == role_run["run_id"]

    # Deleting the chat cancels the role-switch run too.
    assert client.delete("/chats/c1", headers=headers).json()["deleted"] is True
    release.set()
    assert api_module.jobs.registry.get(role_run["run_id"]) is None


def test_role_switch_rejects_non_assistant_or_missing_message(client, api_module):
    user, headers = _user("strict")
    _seed_chat(user["id"])
    for seq in (1, 99):
        response = client.post(
            f"/chats/c1/messages/{seq}/role-answer", json={"role": "manager"}, headers=headers
        )
        assert response.status_code == 404


def test_role_answers_are_deleted_with_the_chat(app_db):
    user = auth.create_user("cleanup", "password123")
    _seed_chat(user["id"])
    chats.save_role_answer(user["id"], "c1", 2, "manager", "Medium", "x")
    assert chats.delete_chat(user["id"], "c1")
    assert chats.list_role_answers(user["id"], "c1") == []


def test_forced_scope_skips_classifier(monkeypatch):
    import os

    os.environ.setdefault("TYPESAFE_API_KEY", "test-key")
    from backend.pipeline import graph

    class _Boom:
        def invoke(self, request):
            raise AssertionError("classifier must not run on a role switch")

    monkeypatch.setattr(graph, "classifier", _Boom())
    out = graph.scope_node({"forced_scope": "find_evidence", "clarify_count": 1})
    assert out["scope"] == "find_evidence" and out["clarify_count"] == 0
