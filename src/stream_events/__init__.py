"""One reducer for the investigation event stream, shared by backend and UI.

The NDJSON stream carries ``messages``, ``tool_results``, ``tool_progress``,
``stage``/``subgraph_name`` and the reasoning events. Two consumers fold it into
``{answer, tool_results, progress}``:

* ``backend.jobs`` -- the state it persists with a finished run, which is what the
  UI shows once the run ends and after a reload;
* ``ui.main_page`` -- the state it renders while the run is live.

They used to be two hand-mirrored copies, and they drifted: the UI learned the
``working`` (sweep progress) and ``skipped`` (refusal) statuses, the backend did
not, so a refusal shown live as "refused" was saved as "failed". The mapping now
lives here once.

The one intended difference is reasoning. Live, ``thinking_delta`` events stream
into an open "Thinking:" line and ``subgraph_message`` only closes it; persisted,
the deltas are skipped and ``subgraph_message`` (the consolidated thought) is
recorded. ``persist`` selects which.

Deliberately neutral: standard library only, no I/O, and no import of
``backend`` or ``ui``, so the thin UI client does not pull in the backend.
"""

from __future__ import annotations

import json
from typing import Any


def new_live_state() -> dict[str, Any]:
    """A fresh, empty state for one investigation."""
    return {"answer": "", "tool_results": [], "progress": []}


def progress_line(op: dict[str, Any]) -> str | None:
    """The progress line for one ``tool_progress`` payload, or ``None`` for none.

    ``started`` and a missing status add nothing; anything not listed is a
    failure.
    """
    action, status = op.get("operation", ""), op.get("status")
    if status in ("started", None):
        return None
    if status == "completed":
        return f"✓ {action}"
    if status == "working":
        # A long-running tool reporting from inside itself (the sweep iterates
        # over up to 1,624 chunks in one call); the message says which pass it is
        # on and what it decided, so it is shown instead of the operation name.
        return f"  → {op.get('message') or action}"
    if status == "skipped":
        # A refusal, not a failure: the query was never run because it was
        # unbounded or unsupported as asked, and it came back with a suggestion.
        return f"↷ {op.get('message') or action}"
    return f"{action} — failed"


def apply_event(state: dict[str, Any], event: dict[str, Any], *, persist: bool) -> None:
    """Fold one streamed event into ``state`` (mutated in place).

    ``state`` holds ``answer``/``tool_results``/``progress``. Unknown event types
    are ignored, and malformed payloads never raise. ``persist=True`` gives the
    stored form (consolidated thoughts), ``False`` the live form (streamed ones).
    """
    etype = event.get("type")
    content = event.get("content", "")
    progress: list[str] = state.setdefault("progress", [])
    if etype == "messages":
        state["answer"] = content
    elif etype == "tool_results":
        try:
            state["tool_results"] = json.loads(content) or []
        except (TypeError, ValueError):
            state["tool_results"] = []
    elif etype == "tool_progress":
        try:
            op = json.loads(content)
        except (TypeError, ValueError):
            return
        if not isinstance(op, dict):
            # Valid JSON that is not an object ("null", "5", "[]") carries no
            # operation or status; treat it as malformed rather than raising.
            return
        line = progress_line(op)
        if line is not None:
            progress.append(line)
    elif etype in ("subgraph_name", "stage"):
        progress.append(f"Currently at stage: {content}")
    elif etype == "thinking_delta":
        if persist:
            return  # the consolidated subgraph_message is what gets stored
        key = event.get("id") or ""
        if state.get("thinking_key") != key:
            progress.append("Thinking: ")
            state["thinking_key"] = key
            state["thinking_at"] = len(progress) - 1
        progress[state["thinking_at"]] += content
    elif etype == "subgraph_message":
        if persist:
            progress.append(f"Thinking: {content}")
        else:
            # Live, the deltas already showed this thought; arriving here closes
            # the open line so the next LLM call starts a new one.
            state["thinking_key"] = None


__all__ = ["apply_event", "new_live_state", "progress_line"]
