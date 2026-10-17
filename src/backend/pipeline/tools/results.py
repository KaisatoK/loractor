"""Collect successful SQL tool results from an agent transcript for display."""

import json
import logging
from typing import Any

from backend.pipeline.tools.envelope import TOOL_KINDS, envelope_metadata

logger = logging.getLogger(__name__)

MAX_RESULT_ROWS = 50


def collect_tool_results(messages) -> list[dict[str, Any]]:
    """Extract successful SQL tool results from a single agent's own messages.

    Builds a ``{tool_call_id: (name, args)}`` map from the agent's
    ``tool_calls`` so each successful tool result can be paired with the call
    that produced it. Each record is assigned its canonical ``R{n}`` id and
    ``row_count`` inline, in the order the results appear, so the id the report
    cites and the id rendered are guaranteed identical. ``row_count`` is the true
    number of returned rows; ``rows`` itself is capped at ``MAX_RESULT_ROWS``.

    Each record also carries the tool-result contract fields (``kind``,
    ``dataset``, ``query``, ``filters``, ``limit``, ``returned``,
    ``total_matches``, ``truncated``; see ``tools/envelope.py``). A legacy
    envelope that lacks a field yields ``None`` for it, never a guess; only
    ``kind`` is inferred from the tool name. ``rows_capped`` marks records whose
    display rows were cut to ``MAX_RESULT_ROWS``.
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
            # Not JSON: a skill read, a todo update, a tool error message, or a
            # database result LangChain could not serialize. Only the last would
            # be lost evidence, so only a database tool's message is a warning.
            name = calls.get(getattr(m, "tool_call_id", None), (None, None))[0]
            log = logger.warning if name in TOOL_KINDS else logger.debug
            log(
                "Skipping non-JSON tool message from %s: %.120s", name, m.content
            )
            continue
        if not isinstance(payload, dict) or payload.get("status") != "success":
            continue
        data = payload.get("data") or []
        if not data:
            continue
        name, args = calls.get(m.tool_call_id, (None, None))
        record = {
            "id": f"R{len(records) + 1}",
            "tool_name": name,
            "arguments": args,
            "row_count": len(data),
            "rows": data[:MAX_RESULT_ROWS],
            "rows_capped": len(data) > MAX_RESULT_ROWS,
            **envelope_metadata(payload, name),
        }
        # The cost of producing this result, when the query layer measured it,
        # so the answer can state how much data was read instead of the model
        # estimating it.
        if payload.get("query_stats"):
            record["query_stats"] = payload["query_stats"]

        # How a long-running tool reached its result, step by step. Only the
        # sweep produces this, and only it needs to: its single call contains a
        # loop over up to 1,624 chunks, so without the trace the result arrives
        # with no account of how it was reached. Streamed too, but the stream is
        # gone once the run ends and these decisions are worth auditing.
        trace = payload.get("progress_trace")
        if trace:
            record["progress_trace"] = trace
        records.append(record)
    return records
