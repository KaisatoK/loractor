"""A user turn whose investigation was stopped is remembered and shown to the agents.

Covers the stored ``stopped`` flag (including upgrading an existing database file),
the API setting it when a run is cancelled, and the transcript label the pipeline
reads, so a later "what was my whole question?" is not mistaken for a fresh
investigation request.
"""

import json
import sqlite3
import threading
import time

from backend.appdb import auth, chats
from backend.jobs import CANCELLED, DONE, ERROR
from backend.pipeline.prompts import GENERAL_SYSTEM_PROMPT
from backend.pipeline.transcript import STOPPED_NOTE, label_stopped


def _user(username: str) -> str:
    return auth.create_user(username, "password123")["id"]


# --- Storage ------------------------------------------------------------------


def test_messages_are_not_stopped_by_default(app_db):
    user = _user("plain")
    chats.create_chat(user, "c1", "engineer")
    assert chats.append_message(user, "c1", "user", "hi")["stopped"] is False
    assert chats.get_messages(user, "c1")[0]["stopped"] is False


def test_mark_message_stopped_flags_only_that_user_message(app_db):
    user = _user("stopper")
    chats.create_chat(user, "c1", "engineer")
    first = chats.append_message(user, "c1", "user", "q1")
    second = chats.append_message(user, "c1", "user", "q2")
    assistant = chats.append_message(user, "c1", "assistant", "a")

    assert chats.mark_message_stopped(user, "c1", first["seq"]) is True
    assert chats.mark_message_stopped(user, "c1", assistant["seq"]) is False

    stopped = {m["seq"]: m["stopped"] for m in chats.get_messages(user, "c1")}
    assert stopped == {first["seq"]: True, second["seq"]: False, assistant["seq"]: False}


def test_mark_message_stopped_on_missing_message_is_false(app_db):
    user = _user("ghost")
    assert chats.mark_message_stopped(user, "nope", 1) is False


def test_existing_database_gains_the_stopped_column(tmp_path, monkeypatch):
    """A database file created before this column existed is upgraded in place."""
    import backend.appdb.database as database

    path = tmp_path / "old.sqlite"
    old = sqlite3.connect(path)
    old.executescript(
        """
        CREATE TABLE messages (
            user_id TEXT NOT NULL, chat_id TEXT NOT NULL, seq INTEGER NOT NULL,
            role TEXT NOT NULL, content TEXT NOT NULL, tool_results TEXT,
            progress TEXT, reframed_for TEXT, created_at TEXT NOT NULL,
            PRIMARY KEY (user_id, chat_id, seq)
        );
        INSERT INTO messages VALUES ('u', 'c', 1, 'user', 'old question', NULL, NULL, NULL, 't');
        """
    )
    old.commit()
    old.close()

    monkeypatch.setenv("APP_DB_PATH", str(path))
    previous = database.connection
    database.connection = None
    try:
        conn = database._connect()
        assert conn.execute("SELECT stopped FROM messages").fetchone()[0] is None
        # Running the upgrade again is a no-op.
        database._migrate(conn)
        assert chats.get_messages("u", "c")[0]["stopped"] is False
    finally:
        if database.connection is not None:
            database.connection.close()
        database.connection = previous


# --- Transcript the agents read -------------------------------------------------


def test_label_stopped_only_marks_stopped_turns():
    assert label_stopped("q", False) == "q"
    assert label_stopped("q", True) == f"q {STOPPED_NOTE}"


def test_transcript_labels_stopped_turns_but_keeps_latest_message_raw(api_module):
    history = [
        {"role": "user", "content": "What recurring problem do we have?", "stopped": True},
        {"role": "user", "content": "Only service X", "stopped": True},
        {"role": "user", "content": "What was my whole question again?"},
    ]
    transcript = api_module._render_conversation(history)
    assert transcript.splitlines() == [
        f"user: What recurring problem do we have? {STOPPED_NOTE}",
        f"user: Only service X {STOPPED_NOTE}",
        "user: What was my whole question again?",
    ]
    # The classifiers' "latest" fields stay real text, not labelled.
    assert api_module._latest(history, "user") == "What was my whole question again?"
    assert api_module._latest(history, "assistant") == ""


def test_general_prompt_explains_stopped_turns():
    assert "stopped by the user" in GENERAL_SYSTEM_PROMPT


# --- API ----------------------------------------------------------------------


def _make_user(username: str) -> tuple[dict, dict]:
    user = auth.create_user(username, "password123")
    token, _ = auth.create_session(user["id"])
    return user, {"Authorization": f"Bearer {token}"}


def _event(**payload) -> str:
    return json.dumps(payload) + "\n"


def _poll_until_terminal(client, headers, chat_id, run_id, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        payload = client.get(
            f"/chats/{chat_id}/run/{run_id}",
            params={"since": 0, "wait": 0.2},
            headers=headers,
        ).json()
        if payload["status"] in (DONE, ERROR, CANCELLED):
            return payload
    raise AssertionError("run did not finish")


def _body(question, history=None):
    return {
        "role": "engineer",
        "effort": "Medium",
        "question": question,
        "history": history or [{"role": "user", "content": question}],
    }


def _wait_flag(client, headers, chat_id, seq, expected, timeout=5.0):
    """The flag is written by the worker just after the run turns terminal."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        messages = client.get(f"/chats/{chat_id}/messages", headers=headers).json()["messages"]
        flag = next(m["stopped"] for m in messages if m["seq"] == seq)
        if flag == expected:
            return messages
        time.sleep(0.02)
    raise AssertionError("stopped flag did not settle")


def test_cancelled_investigation_flags_the_question_and_saves_no_answer(
    client, api_module, monkeypatch
):
    _user_row, headers = _make_user("canceller")
    client.post("/chats", json={"chat_id": "c1", "role": "engineer"}, headers=headers)
    release = threading.Event()

    def slow_stream(_req, _user_id):
        yield _event(type="subgraph_name", content="start")
        release.wait(3)
        yield _event(type="messages", content="never persisted")

    monkeypatch.setattr(api_module, "stream_events", slow_stream)
    run_id = client.post(
        "/chats/c1/investigate", json=_body("What recurring problem?"), headers=headers
    ).json()["run_id"]

    cancelled = client.post(f"/chats/c1/run/{run_id}/cancel", headers=headers)
    assert cancelled.json() == {"cancelled": True}
    release.set()

    assert _poll_until_terminal(client, headers, "c1", run_id)["status"] == CANCELLED
    messages = _wait_flag(client, headers, "c1", 1, True)
    assert [(m["role"], m["stopped"]) for m in messages] == [("user", True)]


def test_completed_investigation_does_not_flag_the_question(client, api_module, monkeypatch):
    _user_row, headers = _make_user("finisher")
    client.post("/chats", json={"chat_id": "c1", "role": "engineer"}, headers=headers)

    def stream(_req, _user_id):
        yield _event(type="messages", content="answer")

    monkeypatch.setattr(api_module, "stream_events", stream)
    run_id = client.post(
        "/chats/c1/investigate", json=_body("q"), headers=headers
    ).json()["run_id"]
    assert _poll_until_terminal(client, headers, "c1", run_id)["status"] == DONE

    messages = client.get("/chats/c1/messages", headers=headers).json()["messages"]
    assert [(m["role"], m["stopped"]) for m in messages] == [
        ("user", False),
        ("assistant", False),
    ]


def test_stopped_flag_in_request_history_reaches_the_pipeline(
    client, api_module, monkeypatch
):
    _user_row, headers = _make_user("history")
    client.post("/chats", json={"chat_id": "c1", "role": "engineer"}, headers=headers)
    seen = {}

    def stream(req, _user_id):
        seen["history"] = req["history"]
        yield _event(type="messages", content="answer")

    monkeypatch.setattr(api_module, "stream_events", stream)
    history = [
        {"role": "user", "content": "first", "stopped": True},
        {"role": "user", "content": "second"},
    ]
    run_id = client.post(
        "/chats/c1/investigate", json=_body("second", history), headers=headers
    ).json()["run_id"]
    _poll_until_terminal(client, headers, "c1", run_id)

    assert seen["history"][0].get("stopped") is True
    assert not seen["history"][1].get("stopped")
