"""Single source of truth for the investigation event contract.

The newline-delimited JSON stream carries five event types. Two consumers fold
those events into derived state with identical semantics:

* ``backend.jobs`` derives the state it persists with each finished run;
* ``ui.main_page`` derives the state it renders while a run is live.

Keeping that mapping in one place is what stops the two layers from drifting.
This module is deliberately neutral: standard library only, no framework
imports, no I/O, and no dependency on either ``backend`` or ``ui``.
"""

from __future__ import annotations

import json

# The five event types on the wire.
EVENT_MESSAGES = "messages"
EVENT_TOOL_RESULTS = "tool_results"
EVENT_TOOL_PROGRESS = "tool_progress"
EVENT_SUBGRAPH_NAME = "subgraph_name"
EVENT_SUBGRAPH_MESSAGE = "subgraph_message"


def new_live_state() -> dict:
    """A fresh, empty live state for one investigation."""
    return {"answer": "", "tool_results": [], "progress": []}


def apply_event(live: dict, event: dict) -> None:
    """Fold one streamed event into ``live`` (mutated in place).

    ``live`` is any mapping holding ``answer``/``tool_results``/``progress``.
    Unknown event types are ignored, and malformed payloads never raise.
    """
    etype = event.get("type")
    content = event.get("content", "")
    if etype == EVENT_MESSAGES:
        live["answer"] = content
    elif etype == EVENT_TOOL_RESULTS:
        try:
            live["tool_results"] = json.loads(content) or []
        except TypeError, ValueError:
            live["tool_results"] = []
    elif etype == EVENT_TOOL_PROGRESS:
        try:
            op = json.loads(content)
        except TypeError, ValueError:
            return
        if not isinstance(op, dict):
            # Valid JSON that is not an object ("null", "5", "[]") carries no
            # operation/status; treat it as malformed rather than raising.
            return
        action, status = op.get("operation", ""), op.get("status")
        if status == "completed":
            live["progress"].append(f"✓ {action}")
        elif status not in ("started", None):
            live["progress"].append(f"{action} — failed")
    elif etype == EVENT_SUBGRAPH_NAME:
        live["progress"].append(f"Currently at stage: {content}")
    elif etype == EVENT_SUBGRAPH_MESSAGE:
        live["progress"].append(f"Thinking: {content}")
