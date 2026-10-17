"""Offline tests for the chat box's lock-and-stop behaviour.

The chat box runs for real in Streamlit's AppTest with the frontend component
replaced by a recorder, so what Python tells the component (``running``,
``stopping``, the stop watermark) is asserted directly; the component's posted
value is simulated by setting the ``chat_box`` session-state entry. The backend
client is stubbed, so no server or browser is needed.
"""

import importlib

import pytest
from streamlit.testing.v1 import AppTest

from ui import chat_client

_SCRIPT = '''
import importlib
import requests
import streamlit as st
from ui import chat_client
from ui.chat_box import chat_box

module = importlib.import_module("ui.chat_box.chat_box")
renders = st.session_state.setdefault("renders", [])
cancels = st.session_state.setdefault("cancels", [])
starts = st.session_state.setdefault("starts", [])

def fake_component(**kwargs):
    renders.append(kwargs)
    return kwargs["default"]

def fake_cancel(**kw):
    cancels.append(kw)
    mode = {cancel!r}
    if mode == "error":
        raise RuntimeError("backend down")
    if mode == "missing":
        response = requests.Response()
        response.status_code = 404
        raise requests.HTTPError(response=response)
    return True

def fake_start(**kw):
    starts.append(kw)
    return {{"run_id": "run-2"}}

module._component = fake_component
chat_client.cancel_run = fake_cancel
chat_client.start_investigation = fake_start
chat_client.auto_complete = lambda token, word: []

if {running!r}:
    st.session_state.setdefault("active_runs", {{"chat-1": {{
        "run_id": "run-1", "since": 0, "answer": "", "progress": [],
        "tool_results": [], "question": "q"}}}})
else:
    st.session_state.setdefault("active_runs", {{}})

current_chat = st.session_state.setdefault("current_chat_obj", {{"role": "engineer", "history": [
    {{"seq": 1, "role": "user", "content": "first", "stopped": True, "run_error": "x"}},
    {{"seq": 2, "role": "user", "content": "second"}},
]}})
chat_box("tok", current_chat, "chat-1", "Medium")
'''


@pytest.fixture(autouse=True)
def _restore_patched_modules(monkeypatch):
    """The script patches module attributes in-process; put them back after."""
    # ``ui.chat_box`` re-exports the function ``chat_box``, shadowing the module.
    module = importlib.import_module("ui.chat_box.chat_box")
    for name in ("cancel_run", "start_investigation", "auto_complete"):
        monkeypatch.setattr(chat_client, name, getattr(chat_client, name))
    monkeypatch.setattr(module, "_component", module._component)


def _app(*, running=True, cancel="ok"):
    return AppTest.from_string(_SCRIPT.format(running=running, cancel=cancel)).run(
        timeout=30
    )


def _click_stop(at, seq=1):
    at.session_state["chat_box"] = {
        "text": "",
        "submitted": "",
        "word": "",
        "submitSeq": 0,
        "stopSeq": seq,
    }
    return at.run(timeout=30)


def _last(at):
    return at.session_state["renders"][-1]


def test_box_is_unlocked_when_nothing_runs():
    at = _app(running=False)
    assert not at.exception
    assert _last(at)["running"] is False
    assert _last(at)["stopping"] is False


def test_box_is_locked_while_a_run_is_active():
    at = _app()
    assert not at.exception
    assert _last(at)["running"] is True
    assert _last(at)["stopping"] is False
    assert _last(at)["suggestions"] == []


def test_stop_cancels_the_run_once_and_marks_it_stopping():
    at = _click_stop(_app())
    assert not at.exception
    assert [c["run_id"] for c in at.session_state["cancels"]] == ["run-1"]
    assert at.session_state["active_runs"]["chat-1"]["stopping"] is True
    assert _last(at)["stopping"] is True
    assert _last(at)["stop_seq_base"] == 1

    # The same click replayed on a later rerun, and a second click while the run
    # is already stopping, must not cancel again.
    at.run(timeout=30)
    _click_stop(at, seq=2)
    assert len(at.session_state["cancels"]) == 1


def test_stop_failure_is_shown_and_leaves_the_run_stoppable():
    at = _click_stop(_app(cancel="error"))
    assert not at.exception
    assert any("Could not stop the investigation" in e.value for e in at.error)
    assert not at.session_state["active_runs"]["chat-1"].get("stopping")
    assert _last(at)["stopping"] is False


def test_stop_on_a_vanished_run_is_left_to_the_poller():
    at = _click_stop(_app(cancel="missing"))
    assert not at.exception
    assert not at.error
    assert not at.session_state["active_runs"]["chat-1"].get("stopping")


def test_stop_without_a_run_does_nothing():
    at = _click_stop(_app(running=False))
    assert not at.exception
    assert at.session_state["cancels"] == []


def test_submit_during_a_run_is_refused():
    at = _app()
    at.session_state["chat_box"] = {
        "text": "",
        "submitted": "again",
        "word": "",
        "submitSeq": 1,
        "stopSeq": 0,
    }
    at.run(timeout=30)
    assert not at.exception
    assert any("already running" in w.value for w in at.warning)
    assert at.session_state["starts"] == []


def test_new_question_sends_stopped_turns_but_no_ui_only_flags():
    at = _app(running=False)
    at.session_state["chat_box"] = {
        "text": "",
        "submitted": "What was my whole question again?",
        "word": "",
        "submitSeq": 1,
        "stopSeq": 0,
    }
    at.run(timeout=30)
    assert not at.exception
    (start,) = at.session_state["starts"]
    assert start["history"] == [
        {"role": "user", "content": "first", "stopped": True},
        {"role": "user", "content": "second"},
        {"role": "user", "content": "What was my whole question again?"},
    ]
