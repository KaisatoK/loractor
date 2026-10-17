"""Offline UI tests for runs that end without an answer (stopped or failed).

``main_page`` runs for real in Streamlit's AppTest; the backend client calls are
stubbed, so no server, database or model is needed.
"""

import pytest
from streamlit.testing.v1 import AppTest

from ui import chat_client


@pytest.fixture(autouse=True)
def _restore_chat_client(monkeypatch):
    """The script stubs the client in-process; put it back after each test."""
    for name in ("get_run", "list_role_answers"):
        monkeypatch.setattr(chat_client, name, getattr(chat_client, name))

_SCRIPT = '''
import streamlit as st
from ui import chat_client
from ui.main_page.main_page import main_page

STATUS = {status!r}

def stub_get_run(**kw):
    return {{"events": [], "next_since": 0, "status": STATUS,
            "error": "boom" if STATUS == "error" else None,
            "result": {{"answer": "", "tool_results": [], "progress": []}}}}

chat_client.get_run = stub_get_run
chat_client.list_role_answers = lambda token, chat_id: []
st.session_state.setdefault("auth", {{"token": "tok", "user": {{"username": "alice"}}}})
st.session_state.setdefault("current_chat", "chat-1")
st.session_state.setdefault("active_runs", {{"chat-1": {{
    "run_id": "run-1", "since": 0, "answer": "", "progress": [],
    "tool_results": [], "question": "Why do spawns fail?"}}}})
st.session_state.setdefault("chat_histories", {{"chat-1": {{"role": "engineer", "history": [
    {{"seq": 1, "role": "user", "content": "Why do spawns fail?", "progress": None}},
], "role_answers": {{}}}}}})
main_page()
'''


def _run(status):
    return AppTest.from_string(_SCRIPT.format(status=status)).run(timeout=30)


def _history(at):
    return at.session_state["chat_histories"]["chat-1"]["history"]


def test_cancelled_run_unlocks_and_marks_the_question_stopped():
    at = _run("cancelled")
    assert not at.exception
    assert at.session_state["active_runs"] == {}
    history = _history(at)
    # A flag on the existing turn, never a new history entry (message numbers
    # must stay in step with the server's).
    assert len(history) == 1
    assert history[0]["stopped"] is True
    assert any("Stopped by user" in c.value for c in at.caption)


def test_failed_run_unlocks_and_keeps_the_error_visible():
    at = _run("error")
    assert not at.exception
    assert at.session_state["active_runs"] == {}
    assert len(_history(at)) == 1
    assert any("Investigation failed: boom" in e.value for e in at.error)
    assert not any("Stopped by user" in c.value for c in at.caption)


def test_done_run_appends_the_answer_without_a_stopped_note():
    at = _run("done")
    assert not at.exception
    assert at.session_state["active_runs"] == {}
    assert [m["role"] for m in _history(at)] == ["user", "assistant"]
    assert not any("Stopped by user" in c.value for c in at.caption)
