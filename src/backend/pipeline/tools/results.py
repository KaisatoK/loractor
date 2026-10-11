"""Collect successful SQL tool results from an agent transcript for display."""

import json

MAX_RESULT_ROWS = 50


def collect_tool_results(messages) -> list[dict]:
    """Extract successful SQL tool results from an agent message list, ready for UI display."""
    calls = {}
    for m in messages:
        for tc in getattr(m, "tool_calls", None) or []:
            calls[tc["id"]] = (tc["name"], tc.get("args", {}))
    records = []
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
