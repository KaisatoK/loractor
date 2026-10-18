"""Offline tests for the live panel's scroll-to-bottom helper.

The auto-scroll component only matters while an answer streams: it is rendered
inside the polling fragment and told how much content there is, so the frontend
can scroll the app to the bottom. The component is replaced by a recorder, so
what Python hands the frontend is asserted directly without a browser.
"""

import importlib

import pytest
from streamlit.testing.v1 import AppTest

from ui import chat_client
from ui.main_page.main_page import _streamed_chars

# A stage, a streamed thought and the answer: the three things that grow while
# an investigation runs.
EVENTS = [
    {"type": "stage", "content": "find_evidence"},
    {"type": "thinking_delta", "id": "m1", "content": "weighing"},
    {"type": "messages", "content": "The answer."},
]

PROGRESS_LINES = ["Currently at stage: find_evidence", "Thinking: weighing"]
EXPECTED_CONTENT_LEN = len("The answer.") + sum(len(line) for line in PROGRESS_LINES)

_SCRIPT = '''
import importlib
import streamlit as st
from ui import chat_client
from ui.main_page.main_page import main_page

module = importlib.import_module("ui.autoscroll.autoscroll")
renders = st.session_state.setdefault("autoscrolls", [])

def fake_component(**kwargs):
    renders.append(kwargs)
    return None

module._component = fake_component

def stub_get_run(**kw):
    return {{"events": {events!r}, "next_since": 1, "status": "running"}}

chat_client.get_run = stub_get_run
st.session_state["auth"] = {{"token": "tok", "user": {{"username": "alice"}}}}
st.session_state["current_chat"] = "chat-1"
st.session_state["chat_histories"] = {{"chat-1": {{"role": "engineer", "history": []}}}}
st.session_state["active_runs"] = {runs!r}
main_page()
'''


@pytest.fixture(autouse=True)
def _restore_patched_modules(monkeypatch):
    """The script patches module attributes in-process; put them back after."""
    module = importlib.import_module("ui.autoscroll.autoscroll")
    monkeypatch.setattr(module, "_component", module._component)
    monkeypatch.setattr(chat_client, "get_run", chat_client.get_run)


def test_running_answer_tells_the_frontend_to_scroll_to_the_bottom():
    runs = {
        "chat-1": {
            "run_id": "run-1",
            "since": 0,
            "answer": "",
            "progress": [],
            "tool_results": [],
            "question": "why did it fail?",
        }
    }
    at = AppTest.from_string(
        _SCRIPT.format(events=EVENTS, runs=runs)
    ).run(timeout=30)

    assert not at.exception
    renders = at.session_state["autoscrolls"]
    assert renders, "the scroll helper must be rendered while a run is live"
    assert renders[-1]["content_len"] == EXPECTED_CONTENT_LEN
    # The run id scopes the "reader scrolled up" release to this investigation.
    assert renders[-1]["run_id"] == "run-1"
    # A stable key keeps the iframe (and its scroll state) mounted across updates.
    assert renders[-1]["key"] == "auto_scroll"
    assert renders[-1]["height"] == 0


def test_no_scroll_helper_without_a_run():
    at = AppTest.from_string(_SCRIPT.format(events=EVENTS, runs={})).run(timeout=30)

    assert not at.exception
    assert at.session_state["autoscrolls"] == []


def test_streamed_chars_grows_while_a_thought_streams_in_place():
    """A streamed thought appends to its line, so a line count would not move.

    The signal must be the rendered text length, or the scroll would fire only
    when a new line appears -- not while the thinking text itself grows.
    """
    short = {"answer": "", "progress": ["Thinking: a"]}
    longer = {"answer": "", "progress": ["Thinking: a longer thought"]}

    assert _streamed_chars(longer) > _streamed_chars(short)
    # And the answer streaming in counts too.
    assert _streamed_chars({"answer": "an answer", "progress": []}) > _streamed_chars(
        {"answer": "", "progress": []}
    )
