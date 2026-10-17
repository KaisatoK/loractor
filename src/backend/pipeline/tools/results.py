"""Collect SQL tool results and tool calls from an agent transcript."""

import json
import logging
from typing import Any

from backend.pipeline.tools.envelope import TOOL_KINDS, envelope_metadata

logger = logging.getLogger(__name__)

# The largest tool cap (top / compare / search), so a capped tool's full result
# survives; the UI table scrolls.
MAX_RESULT_ROWS = 100


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
    display rows were cut to ``MAX_RESULT_ROWS``. ``tool_call_id`` links a record
    to its call (see ``collect_tool_calls``).
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
            "tool_call_id": m.tool_call_id,
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


def _error_text(message, payload: Any) -> str:
    """The failure text of a tool message: an error body, else the content."""
    if isinstance(payload, dict):
        if payload.get("status") == "refused":
            # Refused by the query layer before running (too wide, unsupported).
            text = f"refused: {payload.get('reason')}"
            if payload.get("suggestion"):
                text += f" (suggestion: {payload['suggestion']})"
            return text
        for key in ("error", "error_message", "message"):
            if payload.get(key):
                return str(payload[key])
    content = getattr(message, "content", "")
    return content if isinstance(content, str) else json.dumps(content, default=str)


def collect_tool_calls(messages, records) -> list[dict[str, Any]]:
    """Every database-tool call in the transcript, successful or not.

    ``collect_tool_results`` keeps only successful, non-empty results, so a failed
    or empty call is invisible there. This lists each call to a tool in
    ``envelope.TOOL_KINDS`` (not ``read_file`` / ``write_todos``) in call order as
    ``{call_id, tool_name, arguments, status, error_message, result_ids}``.

    ``status`` is ``"failure"`` when the ``ToolMessage`` has ``status == "error"``
    (``middleware.handle_tool_errors``), carries no result, is not JSON, or its
    envelope ``status`` is not ``"success"``; otherwise ``"success"`` (an empty
    result is a success that produced no record). ``result_ids`` are the ``R{n}``
    of ``records`` whose ``tool_call_id`` equals the call id.
    """
    answers = {
        getattr(m, "tool_call_id", None): m
        for m in messages
        if getattr(m, "type", "") == "tool"
    }
    ids_by_call: dict[Any, list[str]] = {}
    for record in records:
        ids_by_call.setdefault(record.get("tool_call_id"), []).append(record["id"])
    calls: list[dict[str, Any]] = []
    for m in messages:
        for tc in getattr(m, "tool_calls", None) or []:
            if tc["name"] not in TOOL_KINDS:
                continue
            reply = answers.get(tc["id"])
            status, error = "success", None
            if reply is None:
                status, error = "failure", "no tool result was recorded for this call"
            elif getattr(reply, "status", None) == "error":
                status, error = "failure", _error_text(reply, None)
            else:
                try:
                    content = reply.content
                    payload = json.loads(content) if isinstance(content, str) else content
                except Exception:
                    status, error = "failure", "tool result was not valid JSON"
                else:
                    if not isinstance(payload, dict) or payload.get("status") != "success":
                        status, error = "failure", _error_text(reply, payload)
            calls.append(
                {
                    "call_id": tc["id"],
                    "tool_name": tc["name"],
                    "arguments": tc.get("args", {}),
                    "status": status,
                    "error_message": error,
                    "result_ids": ids_by_call.get(tc["id"], []),
                }
            )
    return calls
