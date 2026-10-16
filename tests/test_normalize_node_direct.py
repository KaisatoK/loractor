"""``normalize`` generator and direct ``normalize_node`` invocation tests.

``normalize_node`` announces its stage through ``graph._emit_stage``, which calls
``langgraph.config.get_stream_writer``. That raises
``RuntimeError: Called get_config outside of a runnable context`` when a node is
called directly (as several tests in this suite do), so ``_emit_stage`` guards
the call. The first test proves the guard holds and a direct call still returns a
usable normalized payload -- with the model stubbed, so nothing here makes a live
call.

The rest cover ``generate_normalized`` itself: it makes a direct model call (no
``response_format``, so the reasoning streams incrementally) and validates the
message's text locally with no fence handling. The model is stubbed throughout.

The shared ``tests/conftest.py`` installs the other dummy provider keys before
the backend is imported; importing ``graph`` additionally needs a
``TYPESAFE_API_KEY`` to exist, so a dummy value is set at the top.
"""

import json
import os

os.environ.setdefault("TYPESAFE_API_KEY", "test-key")

import pytest  # noqa: E402
from langchain_core.messages import AIMessage  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from backend.pipeline import graph  # noqa: E402
from backend.pipeline.agents import normalize as normalize_module  # noqa: E402
from backend.pipeline.contracts import NormalizedOutput  # noqa: E402


def test_normalize_node_direct_call_outside_runnable_context(monkeypatch):
    # ``_emit_stage`` must swallow the RuntimeError a direct call raises; the
    # prompt builder and the model entry point are stubbed so no live call runs.
    monkeypatch.setattr(graph.prompts, "normalize", lambda state: "normalize this")

    def fake_generate_normalized(_prompt):
        return NormalizedOutput(
            normalized_question="count the errors",
            intent="count the errors in the logs",
            context={},
            ambiguities=[],
        )

    monkeypatch.setattr(graph, "generate_normalized", fake_generate_normalized)

    state = {
        "conversation": "user: count the errors",
        "latest_message": "count the errors",
        "latest_answer": "",
        "role": {"name": "engineer", "objective": "investigate a specific error"},
        "effort": "Low",
        "answer": "",
    }

    result = graph.normalize_node(state)

    assert result["normalized"]["normalized_question"] == "count the errors"
    assert result["normalized"]["intent"] == "count the errors in the logs"
    assert result["revision_count"] == 0


# --- generate_normalized (direct call + local validation) ------------------


class _RecordingModel:
    """A model stand-in that captures its call and returns a fabricated reply."""

    def __init__(self, message):
        self.message = message
        self.messages = None
        self.kwargs = None

    def invoke(self, messages, **kwargs):
        self.messages = messages
        self.kwargs = kwargs
        return self.message


_NORMALIZED_JSON = json.dumps(
    {
        "normalized_question": "count the errors",
        "intent": "count the errors in the logs",
        "context": {},
        "ambiguities": [],
    }
)


def test_generate_normalized_parses_text_without_response_format(monkeypatch):
    # The streamed reasoning projects incrementally only when the call carries no
    # structured-output guarantee; ``response_format`` (in any form) collapses it
    # to a single delta. Pin its absence so it cannot be reintroduced silently.
    message = AIMessage(
        content=[
            {"type": "reasoning", "reasoning": "weighing the question"},
            {"type": "text", "text": _NORMALIZED_JSON},
        ]
    )
    recorder = _RecordingModel(message)
    monkeypatch.setattr(normalize_module, "deepseek", recorder)

    normalized = normalize_module.generate_normalized("count the errors")

    # ``message.text`` excludes the reasoning block, so only the json text block
    # is parsed into the contract.
    assert normalized == NormalizedOutput(
        normalized_question="count the errors",
        intent="count the errors in the logs",
        context={},
        ambiguities=[],
    )
    assert recorder.kwargs == {}
    assert "response_format" not in recorder.kwargs


def test_generate_normalized_malformed_text_raises_validation_error(monkeypatch):
    # No fence handling or repair: text that is not valid json for the contract
    # raises rather than being swallowed.
    recorder = _RecordingModel(AIMessage(content="not json at all"))
    monkeypatch.setattr(normalize_module, "deepseek", recorder)

    with pytest.raises(ValidationError):
        normalize_module.generate_normalized("count the errors")
