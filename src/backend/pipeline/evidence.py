"""Build the evidence items of a report in code, from the actual tool calls.

The model used to re-type every tool call (name, parameters, status, errors,
result ids), which it could get wrong ("available" with no results, a status
synonym, an invented id). Code already has all of that exactly, so each call
becomes one evidence item here and the model only writes the judgement.
"""

from collections.abc import Sequence

from backend.pipeline.contracts import Evidence, ToolStatus
from backend.pipeline.tools.envelope import TOOL_KINDS


def build_evidence(calls: Sequence[dict], records: Sequence[dict]) -> list[Evidence]:
    """One evidence item per call, ids ``E1..En`` in call order.

    ``calls`` come from ``tools.results.collect_tool_calls``. An item is
    ``available`` when its call produced a result record, else ``unavailable``
    (the call failed or returned no rows). ``records`` are the captured results;
    the link is the call's own ``result_ids``.
    """
    known = {record.get("id") for record in records}
    items = []
    for number, call in enumerate(calls, start=1):
        result_ids = [rid for rid in call.get("result_ids", []) if rid in known]
        kind = _kind(call, records)
        items.append(
            Evidence(
                id=f"E{number}",
                name=call["tool_name"],
                description=f"{kind} query: {call.get('arguments')}",
                tool_used=[
                    ToolStatus(
                        name=call["tool_name"],
                        parameters=call.get("arguments") or {},
                        status=call["status"],
                        error_message=call.get("error_message"),
                    )
                ],
                availability="available" if result_ids else "unavailable",
                result_ids=result_ids,
            )
        )
    return items


def _kind(call: dict, records: Sequence[dict]) -> str:
    """The tool-result kind of a call (from its record, else the tool name)."""
    for record in records:
        if record.get("id") in call.get("result_ids", []) and record.get("kind"):
            return record["kind"]
    return TOOL_KINDS.get(call["tool_name"], "unknown")


def evidence_lines(evidence: Sequence[Evidence], records: Sequence[dict]) -> str:
    """One prompt line per item, e.g. ``- E2: get_trend_tool({...}) -> R2 [trend, 15 rows]``."""
    by_id = {record.get("id"): record for record in records}
    lines = []
    for item in evidence:
        call = item.tool_used[0]
        head = f"- {item.id}: {call.name}({call.parameters})"
        if call.status == "failure":
            lines.append(f"{head} -> failed: {call.error_message}")
        elif call.status == "refused":
            lines.append(f"{head} -> refused: {call.error_message}")
        elif not item.result_ids:
            lines.append(f"{head} -> no rows")
        else:
            parts = []
            for rid in item.result_ids:
                record = by_id.get(rid, {})
                kind = record.get("kind") or "unknown"
                flag = " truncated" if record.get("truncated") else ""
                parts.append(f"{rid} [{kind}{flag}, {record.get('row_count')} rows]")
            lines.append(f"{head} -> {', '.join(parts)}")
    return "\n".join(lines)
