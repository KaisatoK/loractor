"""Tests for the shared investigation-event reducer.

``stream_events.apply_event`` is the single source of truth for turning the
newline-delimited JSON stream into ``answer``/``tool_results``/``progress``.
The backend (``backend.jobs``) and the Streamlit UI (``ui.main_page``) both call
it, so these tests pin the per-type semantics and, crucially, the cross-layer
contract: the same events must derive identical state on both sides.
"""

import json

from backend.jobs import RunRegistry, RunState
from stream_events import (
    EVENT_MESSAGES,
    EVENT_SUBGRAPH_MESSAGE,
    EVENT_SUBGRAPH_NAME,
    EVENT_TOOL_PROGRESS,
    EVENT_TOOL_RESULTS,
    apply_event,
    new_live_state,
)


def _ui_apply(events: list[dict]) -> dict:
    """Drive the reducer the way the UI does: a plain dict of live state."""
    live = new_live_state()
    for event in events:
        apply_event(live, event)
    return live


def _backend_apply(events: list[dict]) -> RunState:
    """Drive the reducer the way the backend does: through the run registry."""
    registry = RunRegistry()
    run = RunState(run_id="r", user_id="u", chat_id="c")
    for event in events:
        registry._append(run, json.dumps(event) + "\n")
    return run


# --- per-event-type derived state ---------------------------------------------


def test_new_live_state_is_empty_and_independent():
    first = new_live_state()
    assert first == {"answer": "", "tool_results": [], "progress": []}
    # Each call must hand back fresh containers, not shared ones.
    first["progress"].append("x")
    assert new_live_state()["progress"] == []


def test_messages_sets_answer():
    live = _ui_apply([{"type": EVENT_MESSAGES, "content": "Findings [[RESULT:R1]]"}])
    assert live["answer"] == "Findings [[RESULT:R1]]"
    assert live["progress"] == []


def test_tool_results_parses_json_list():
    payload = [{"id": "R1", "rows": [{"a": 1}]}]
    live = _ui_apply([{"type": EVENT_TOOL_RESULTS, "content": json.dumps(payload)}])
    assert live["tool_results"] == payload


def test_tool_results_empty_when_null():
    live = _ui_apply([{"type": EVENT_TOOL_RESULTS, "content": "null"}])
    assert live["tool_results"] == []


def test_tool_progress_records_completed_and_failed():
    live = _ui_apply(
        [
            {
                "type": EVENT_TOOL_PROGRESS,
                "content": json.dumps({"operation": "count", "status": "started"}),
            },
            {
                "type": EVENT_TOOL_PROGRESS,
                "content": json.dumps({"operation": "count", "status": "completed"}),
            },
            {
                "type": EVENT_TOOL_PROGRESS,
                "content": json.dumps({"operation": "search", "status": "failed"}),
            },
        ]
    )
    # "started" is silent; completed gets a check, other terminal states a note.
    assert live["progress"] == ["✓ count", "search — failed"]


def test_subgraph_name_appends_stage():
    live = _ui_apply([{"type": EVENT_SUBGRAPH_NAME, "content": "normalize"}])
    assert live["progress"] == ["Currently at stage: normalize"]


def test_subgraph_message_appends_thinking():
    live = _ui_apply([{"type": EVENT_SUBGRAPH_MESSAGE, "content": "checking logs"}])
    assert live["progress"] == ["Thinking: checking logs"]


def test_unknown_event_type_is_ignored():
    live = _ui_apply([{"type": "not_a_real_type", "content": "x"}])
    assert live == {"answer": "", "tool_results": [], "progress": []}


# --- cross-layer contract -----------------------------------------------------


def test_backend_and_ui_derive_identical_state():
    """The registry path and the UI path must agree event-for-event."""
    events = [
        {"type": EVENT_SUBGRAPH_NAME, "content": "normalize"},
        {"type": EVENT_SUBGRAPH_MESSAGE, "content": "planning"},
        {
            "type": EVENT_TOOL_PROGRESS,
            "content": json.dumps({"operation": "count", "status": "started"}),
        },
        {
            "type": EVENT_TOOL_PROGRESS,
            "content": json.dumps({"operation": "count", "status": "completed"}),
        },
        {
            "type": EVENT_TOOL_RESULTS,
            "content": json.dumps([{"id": "R1", "rows": [{"a": 1}]}]),
        },
        {"type": EVENT_MESSAGES, "content": "Findings [[RESULT:R1]]"},
        # A malformed payload must be tolerated identically by both layers.
        {"type": EVENT_TOOL_RESULTS, "content": "{not json"},
        {"type": EVENT_TOOL_PROGRESS, "content": "{not json"},
    ]

    backend = _backend_apply(events)
    ui = _ui_apply(events)

    assert backend.answer == ui["answer"]
    assert backend.tool_results == ui["tool_results"]
    assert backend.progress == ui["progress"]

    # And the agreed-upon values are the expected ones, so this is not passing
    # merely because both sides are equally broken.
    assert backend.answer == "Findings [[RESULT:R1]]"
    assert backend.tool_results == []
    assert backend.progress == [
        "Currently at stage: normalize",
        "Thinking: planning",
        "✓ count",
    ]


# --- tolerance ----------------------------------------------------------------


def test_malformed_json_never_raises():
    for event in (
        {"type": EVENT_TOOL_RESULTS, "content": "{not json"},
        {"type": EVENT_TOOL_RESULTS, "content": None},
        {"type": EVENT_TOOL_PROGRESS, "content": "{not json"},
        {"type": EVENT_TOOL_PROGRESS, "content": None},
    ):
        live = new_live_state()
        apply_event(live, event)  # must not raise

    # A broken results payload resets tool_results rather than leaving it stale.
    live = _ui_apply([{"type": EVENT_TOOL_RESULTS, "content": "{not json"}])
    assert live["tool_results"] == []


def test_non_object_json_payload_never_raises():
    """Valid JSON that is not an object must be tolerated, not raise.

    ``json.loads`` happily accepts ``"null"``/``"5"``/``"[]"``; the
    ``tool_progress`` branch must recognise these carry no operation/status and
    return instead of calling ``.get`` on a non-mapping (which raised
    ``AttributeError`` and, in the backend, dropped the run's event).
    """
    for content in ("null", "5", "[]"):
        # UI path.
        live = new_live_state()
        apply_event(live, {"type": EVENT_TOOL_PROGRESS, "content": content})
        assert live["progress"] == []
        # Backend path: the same payload must not escape the run's worker.
        run = _backend_apply([{"type": EVENT_TOOL_PROGRESS, "content": content}])
        assert run.progress == []
