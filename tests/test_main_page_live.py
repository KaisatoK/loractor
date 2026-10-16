"""Offline UI regression tests for the live investigation panel.

``render_active_run`` groups a run's progress lines under the "Currently at
stage" markers the backend emits. A run can stream progress with no stage
marker at all (``normalize`` and ``answer`` no longer surface a subgraph), which
used to crash the panel with ``IndexError: list index out of range``. These
tests drive the real ``ui.main_page.main_page`` path with ``chat_client.get_run``
stubbed, so they need no database and no model calls.
"""

import json

from streamlit.testing.v1 import AppTest

# Progress with no stage marker: the state that crashed ``render_active_run``.
NO_STAGE_EVENTS = [
    {
        "type": "tool_progress",
        "content": json.dumps({"operation": "count", "status": "completed"}),
    }
]

# A stage starting (the normalize subgraph), then its tool call completing.
STAGE_EVENTS = [{"type": "subgraph_name", "content": "normalize"}] + NO_STAGE_EVENTS

# A root-scope stage announcement (``stage`` event) followed by that stage's
# reasoning: the two root-scope events normalize/answer now emit.
STAGE_THINKING_EVENTS = [
    {"type": "stage", "content": "normalize"},
    {"type": "thinking_delta", "id": "m1", "content": "weighing the question"},
    {"type": "subgraph_message", "content": "weighing the question"},
]


def _script(events):
    """Build the AppTest script for a run emitting ``events``.

    ``main_page`` and ``render_active_run`` run for real; only ``get_run`` is
    replaced, so the panel renders from the supplied events.
    """
    return f'''
import streamlit as st
from ui import chat_client
from ui.main_page.main_page import main_page


def stub_get_run(**kw):
    return {{
        "events": {events!r},
        "next_since": 1,
        "status": "running",
    }}


chat_client.get_run = stub_get_run
st.session_state["auth"] = {{"token": "tok", "user": {{"username": "alice"}}}}
st.session_state["current_chat"] = "chat-1"
st.session_state["chat_histories"] = {{"chat-1": {{"role": "engineer", "history": []}}}}
st.session_state["active_runs"] = {{
    "chat-1": {{"run_id": "run-1", "since": 0, "answer": "",
               "progress": [], "tool_results": [], "question": "why did it fail?"}}
}}
main_page()
'''


def _expander_labels(at):
    return [expander.label for expander in at.expander]


def _captions(block):
    return [caption.value for caption in block.caption]


def test_run_without_stage_marker_renders_progress():
    """No stage marker must not crash, and the progress must still render."""
    at = AppTest.from_string(_script(NO_STAGE_EVENTS)).run(timeout=30)

    assert not at.exception
    # With no stage started yet the panel falls back to its pre-grouping label
    # and shows the progress as plain captions.
    assert _expander_labels(at) == ["Processing..."]
    assert "✓ count" in _captions(at.expander[0])


def test_run_with_stage_marker_groups_progress_under_the_stage():
    """A stage marker keeps the grouped rendering unchanged."""
    at = AppTest.from_string(_script(STAGE_EVENTS)).run(timeout=30)

    assert not at.exception
    assert _expander_labels(at) == ["Currently at stage: normalize"]
    # The stage marker is the expander label; the completed tool call is grouped
    # under it as the expander's caption.
    assert "✓ count" in _captions(at.expander[0])


def test_root_stage_event_groups_its_thinking_under_the_stage():
    """A root-scope ``stage`` event labels the panel and nests its thought."""
    at = AppTest.from_string(_script(STAGE_THINKING_EVENTS)).run(timeout=30)

    assert not at.exception
    assert _expander_labels(at) == ["Currently at stage: normalize"]
    # The reducer prepends "Thinking: " to the streamed reasoning text.
    assert _captions(at.expander[0]) == ["Thinking: weighing the question"]
