"""Collect successful SQL tool results from an agent transcript for display."""

import json
from typing import Any

MAX_RESULT_ROWS = 50


def collect_tool_results(messages) -> list[dict[str, Any]]:
    """Extract successful SQL tool results from a single agent's own messages.

    Builds a ``{tool_call_id: (name, args)}`` map from the agent's
    ``tool_calls`` so each successful tool result can be paired with the call
    that produced it. Each record is assigned its canonical ``R{n}`` id and
    ``row_count`` inline, in the order the results appear, so the id the report
    cites and the id rendered are guaranteed identical. ``row_count`` is the true
    number of returned rows; ``rows`` itself is capped at ``MAX_RESULT_ROWS``.
    """
    calls: dict[Any, tuple[Any, Any]] = {}
    for m in messages:
        for tc in getattr(m, "tool_calls", None) or []:
            calls[tc["id"]] = (tc["name"], tc.get("args", {}))
    records: list[dict[str, Any]] = []
    for m in messages:
        if getattr(m, "type", "") != "tool":
            continue
        try:
            payload = json.loads(m.content) if isinstance(m.content, str) else m.content
        except Exception:
            continue
        if not isinstance(payload, dict) or payload.get("status") != "success":
            continue
        data = payload.get("data") or []
        if not data:
            continue
        name, args = calls.get(m.tool_call_id, (None, None))
        records.append(
            {
                "id": f"R{len(records) + 1}",
                "tool_name": name,
                "arguments": args,
                "row_count": len(data),
                "rows": data[:MAX_RESULT_ROWS],
            }
        )
    return records
