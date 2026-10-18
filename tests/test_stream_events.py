"""Offline tests for the shared investigation-event reducer (``stream_events``)."""

import json
import os

os.environ.setdefault("TYPESAFE_API_KEY", "test-key")

import pytest

from backend.jobs import RunRegistry, RunState
from stream_events import apply_event, new_live_state, progress_line
from ui.main_page.main_page import _apply_event as ui_apply_event


def _progress(operation, status, message=None):
    payload = {"operation": operation, "status": status}
    if message:
        payload["message"] = message
    return {"type": "tool_progress", "content": json.dumps(payload)}


# Every event type the backend emits, except the reasoning ones (whose live and
# stored forms differ on purpose; see test_reasoning_*).
STREAM = [
    {"type": "stage", "content": "find_evidence"},
    _progress("count logs", "started"),
    _progress("count logs", "completed"),
    _progress("search otel_s3", "skipped", "refused: window too wide"),
    _progress("sweep otel_s3", "working", "[profile] 120/1,624 chunks"),
    _progress("get trend", "failed"),
    {"type": "tool_results", "content": json.dumps([{"id": "R1"}])},
    {"type": "messages", "content": "The answer."},
]


def _persisted(events):
    run = RunState(run_id="r", user_id="u", chat_id="c")
    for event in events:
        RunRegistry._track(None, run, event)
    return {"answer": run.answer, "tool_results": run.tool_results, "progress": run.progress}


def _live(events):
    state = new_live_state()
    for event in events:
        ui_apply_event(state, event)
    return state


def test_backend_and_ui_record_the_same_progress():
    # The drift this module removes: a refusal or sweep step shown live as such
    # was saved (and shown after the run) as "failed".
    persisted, live = _persisted(STREAM), _live(STREAM)
    assert persisted["progress"] == live["progress"]
    assert persisted["answer"] == live["answer"] == "The answer."
    assert persisted["tool_results"] == live["tool_results"] == [{"id": "R1"}]


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("started", None),
        (None, None),
        ("completed", "✓ count logs"),
        ("working", "  → [profile] 120/1,624"),
        ("skipped", "↷ refused: window too wide"),
        ("failed", "count logs — failed"),
    ],
)
def test_progress_line_per_status(status, expected):
    message = {"working": "[profile] 120/1,624", "skipped": "refused: window too wide"}
    op = {"operation": "count logs", "status": status}
    if status in message:
        op["message"] = message[status]
    assert progress_line(op) == expected


@pytest.mark.parametrize(
    ("extra", "expected"),
    [
        ({"returned": 10, "total_matches": 1234, "cost": "12 ms"},
         "✓ search — 10 of 1,234 rows · 12 ms"),
        ({"returned": 1, "total_matches": 1}, "✓ search — 1 row"),
        ({"returned": 0, "total_matches": None}, "✓ search — 0 rows"),
        ({"cost": "~6,327,325 rows scanned, 2110 ms"},
         "✓ search — ~6,327,325 rows scanned, 2110 ms"),
        # Unknown or malformed details fall back to the bare line.
        ({"returned": None, "total_matches": None}, "✓ search"),
        ({"returned": True, "cost": "  "}, "✓ search"),
    ],
)
def test_completed_line_shows_rows_and_cost(extra, expected):
    assert progress_line({"operation": "search", "status": "completed", **extra}) == expected


def test_failed_line_shows_the_reason():
    op = {"operation": "get trend", "status": "failed", "error": "statement timeout"}
    assert progress_line(op) == "get trend — failed: statement timeout"
    assert progress_line({**op, "error": " "}) == "get trend — failed"


def test_refusal_is_never_saved_as_a_failure():
    progress = _persisted([_progress("search", "skipped", "refused: too wide")])["progress"]
    assert progress == ["↷ refused: too wide"]
    assert not any("failed" in line for line in progress)


@pytest.mark.parametrize("content", ["null", "5", "[]", '"text"', "not json", None])
def test_malformed_tool_progress_never_raises(content):
    for persist in (True, False):
        state = new_live_state()
        apply_event(state, {"type": "tool_progress", "content": content}, persist=persist)
        assert state["progress"] == []


def test_unknown_event_types_are_ignored():
    state = new_live_state()
    apply_event(state, {"type": "something_new", "content": "x"}, persist=True)
    assert state == new_live_state()


def test_bad_tool_results_become_empty():
    state = new_live_state()
    apply_event(state, {"type": "tool_results", "content": "{broken"}, persist=False)
    assert state["tool_results"] == []


def test_reasoning_streams_live_and_is_stored_consolidated():
    events = [
        {"type": "thinking_delta", "id": "m1", "content": "Weigh "},
        {"type": "thinking_delta", "id": "m1", "content": "the counts."},
        {"type": "subgraph_message", "content": "Weigh the counts."},
        {"type": "thinking_delta", "id": "m2", "content": "Next."},
    ]
    assert _live(events)["progress"] == ["Thinking: Weigh the counts.", "Thinking: Next."]
    assert _persisted(events)["progress"] == ["Thinking: Weigh the counts."]


def test_reducer_does_not_import_backend_or_ui():
    # The UI is a thin client; the shared module must not pull in either layer.
    import ast
    import stream_events

    tree = ast.parse(open(stream_events.__file__, encoding="utf-8").read())
    imported = {
        (node.module or "").split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    } | {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert not imported & {"backend", "ui", "streamlit", "fastapi"}
