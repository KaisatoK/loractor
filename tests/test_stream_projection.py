"""Regression: the root ``messages`` branch must tolerate *any* item shape.

Under langgraph's v3 streaming protocol a root-scope LLM call surfaces on the
``messages`` channel as a single ``ChatModelStream`` object, not a list of
messages. ``api.stream_events`` used to do ``item[-1].text`` there, which raises
``TypeError: 'ChatModelStream' object is not subscriptable`` and tears the NDJSON
response before the terminal ``messages`` event.

This drives the *real* ``stream_events`` generator with a stubbed run that yields
a genuine ``ChatModelStream`` on ``messages`` followed by a ``values`` event, and
asserts the answer still reaches the client. It needs no model and no database.
"""

import json
import os

# Must precede the (fixture-triggered) import of ``backend.api``.
os.environ.setdefault("TYPESAFE_API_KEY", "test-key")
os.environ.setdefault("GOOGLE_API_KEY", "test-key")
os.environ.setdefault("DEEPSEEK_API_KEY", "test-key")
os.environ.setdefault("GROQ_API_KEY", "test-key")

from langgraph.stream.transformers import ChatModelStream  # noqa: E402


class _FakeRun:
    """Minimal stand-in for the pipeline's interleaved stream handle."""

    def __init__(self, events):
        self._events = events

    def interleave(self, *channels):
        return iter(self._events)


class _FakeChatbot:
    def __init__(self, events):
        self._events = events

    def stream_events(self, *args, **kwargs):
        return _FakeRun(self._events)


def _request():
    return {
        "chat_id": "c1",
        "role": "engineer",
        "effort": "Low",
        "history": [{"role": "user", "content": "why did ingest fail?"}],
    }


def test_root_messages_branch_survives_chat_model_stream(monkeypatch, api_module):
    # A root-scope LLM call: exactly the shape that broke blind indexing.
    monkeypatch.setattr(
        api_module,
        "chatbot",
        _FakeChatbot(
            [
                ("messages", ChatModelStream()),
                ("values", {"answer": "grounded answer"}),
            ]
        ),
    )

    events = list(api_module.stream_events(_request(), "user-1"))

    # The generator did not raise, and the terminal answer event was emitted.
    final = json.loads(events[-1])
    assert final == {"type": "messages", "content": "grounded answer"}


def test_root_messages_branch_still_logs_list_shaped_items(monkeypatch, api_module, caplog):
    """The diagnostic intent survives: an indexable item is still logged."""

    class _Msg:
        text = "partial"

    monkeypatch.setattr(
        api_module,
        "chatbot",
        _FakeChatbot([("messages", [_Msg()]), ("values", {"answer": "ok"})]),
    )

    with caplog.at_level("DEBUG", logger="backend.api"):
        list(api_module.stream_events(_request(), "user-1"))

    assert "stream message: partial" in caplog.text


def _reasoning_stream(message_id, deltas):
    """A real ``ChatModelStream`` seeded with reasoning deltas (offline)."""
    stream = ChatModelStream(message_id=message_id)
    for delta in deltas:
        stream.dispatch(
            {
                "event": "content-block-delta",
                "index": 0,
                "delta": {"type": "reasoning-delta", "reasoning": delta},
            }
        )
    return stream


def test_root_messages_branch_forwards_reasoning(monkeypatch, api_module):
    """A root-scope call's reasoning becomes thinking_deltas plus one message."""
    stream = _reasoning_stream("m1", ["weigh ", "the ", "question"])
    monkeypatch.setattr(
        api_module,
        "chatbot",
        _FakeChatbot([("messages", stream), ("values", {"answer": "grounded"})]),
    )

    events = [
        json.loads(line) for line in api_module.stream_events(_request(), "user-1")
    ]

    thinking = [e for e in events if e["type"] == "thinking_delta"]
    assert [(e["id"], e["content"]) for e in thinking] == [
        ("m1", "weigh "),
        ("m1", "the "),
        ("m1", "question"),
    ]
    # Exactly one consolidated message, mirroring the subgraphs branch.
    assert [e for e in events if e["type"] == "subgraph_message"] == [
        {"type": "subgraph_message", "content": "weigh the question"}
    ]
    # The terminal answer is still delivered from ``values``.
    assert events[-1] == {"type": "messages", "content": "grounded"}


def test_root_messages_branch_skips_empty_reasoning(monkeypatch, api_module):
    """A root call with no reasoning must not append a bare thinking line."""
    monkeypatch.setattr(
        api_module,
        "chatbot",
        _FakeChatbot(
            [("messages", ChatModelStream()), ("values", {"answer": "ok"})]
        ),
    )

    events = [
        json.loads(line) for line in api_module.stream_events(_request(), "user-1")
    ]

    assert [e for e in events if e["type"] == "thinking_delta"] == []
    assert [e for e in events if e["type"] == "subgraph_message"] == []


def test_root_custom_branch_translates_node_stage(monkeypatch, api_module):
    """A root-scope ``node_stage`` custom event becomes a UI ``stage`` event."""
    monkeypatch.setattr(
        api_module,
        "chatbot",
        _FakeChatbot(
            [
                ("custom", {"type": "node_stage", "name": "normalize"}),
                ("values", {"answer": "ok"}),
            ]
        ),
    )

    events = [
        json.loads(line) for line in api_module.stream_events(_request(), "user-1")
    ]

    assert {"type": "stage", "content": "normalize"} in events
    # tool_progress forwarding on the same branch is unchanged.
    assert events[-1] == {"type": "messages", "content": "ok"}

