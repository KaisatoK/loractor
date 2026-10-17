"""Invoke a tool the way the agent's ToolNode does, offline.

ToolNode builds a real ``ToolRuntime`` and passes it in the call's ``args``;
langchain-core then parses the input against the tool's schema. Calling a
tool's ``.func`` directly skips that parsing, which is how a tool that dropped
its injected runtime on every real call still passed its tests.
"""

from __future__ import annotations

from langchain.tools import ToolRuntime


def tool_call(tool, events: list, **args) -> dict:
    """A ToolCall for ``tool`` with a real runtime whose stream writes to ``events``."""
    runtime = ToolRuntime(
        state={}, tool_call_id="call-1", config={}, context=None, store=None,
        stream_writer=events.append, tools=[],
    )
    return {
        "type": "tool_call",
        "name": tool.name,
        "id": "call-1",
        "args": {**args, "runtime": runtime},
    }
