"""Graphviz DOT for the engineer's before -> anchor -> after chain.

Built from the derived ``timeline`` record (``backend.pipeline.timeline``), never by
a model: the answer still only cites ``[[RESULT:Rn]]`` and the table stays the
source of truth. This module is pure text-in, text-out (stdlib only, no Streamlit,
no backend import), so the diagram is testable without a runtime.

Log text is untrusted, so no message text is drawn at all: a node shows only the
offset, the seconds from the anchor, the severity and the service, each escaped for
a quoted DOT string. Node ids are generated (``n0``, ``n1``...), never taken from
the data.
"""

from __future__ import annotations

from typing import Any

TIMELINE_TOOL_NAME = "timeline"
# Most nodes drawn. The record is already capped at this many rows by the backend.
MAX_NODES = 100
_LABEL_CHARS = 40

_ANCHOR = 'style="filled,bold" fillcolor="#ffd6d6" color="#c0392b"'
_SAME_REQUEST = 'style="filled" fillcolor="#e3f0ff" color="#2e6da4"'
_PLAIN = 'style="filled" fillcolor="#f5f5f5" color="#999999"'


def _escape(value: Any) -> str:
    """Make ``value`` safe inside a double-quoted DOT string.

    Backslashes and quotes are escaped, control characters (newlines included) become
    spaces, and the text is cut so one long service name cannot widen the chart.
    """
    text = "" if value is None else str(value)
    text = "".join(" " if ord(ch) < 32 or ord(ch) == 127 else ch for ch in text)
    if len(text) > _LABEL_CHARS:
        text = text[: _LABEL_CHARS - 1] + "…"
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _signed(value: Any, *, suffix: str = "") -> str:
    """``+3`` / ``-2`` / ``0``; a missing or non-numeric value is ``?``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "?"
    if value == 0:
        return f"0{suffix}"
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return f"{value:+}{suffix}"


def _record_id(row: dict) -> int:
    value = row.get("record_id")
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _is_anchor(row: dict, anchor_record_id: Any) -> bool:
    if anchor_record_id is not None and row.get("record_id") == anchor_record_id:
        return True
    return anchor_record_id is None and row.get("offset") == 0


def timeline_dot(record: dict) -> str | None:
    """DOT text for a ``timeline`` record, or ``None`` when there is nothing to draw.

    One node per row in record-id order (the table's order), joined by edges; the
    anchor is highlighted and rows on the anchor's request are marked.
    """
    if not isinstance(record, dict) or record.get("tool_name") != TIMELINE_TOOL_NAME:
        return None
    rows = [r for r in (record.get("rows") or []) if isinstance(r, dict)]
    if len(rows) < 2:
        return None
    rows = sorted(rows, key=_record_id)[:MAX_NODES]
    arguments = record.get("arguments")
    anchor_id = arguments.get("anchor_record_id") if isinstance(arguments, dict) else None

    lines = [
        "digraph timeline {",
        '  rankdir="TB";',
        '  node [shape="box" fontname="Helvetica" fontsize="11"];',
        '  edge [color="#666666"];',
    ]
    for index, row in enumerate(rows):
        label = (
            f"{_escape(_signed(row.get('offset')))} lines · "
            f"{_escape(_signed(row.get('seconds'), suffix='s'))}\\n"
            f"{_escape(row.get('severity'))} · {_escape(row.get('service'))}"
        )
        if _is_anchor(row, anchor_id):
            attrs = _ANCHOR
            label = f"ANCHOR\\n{label}"
        elif row.get("same_request") is True:
            attrs = _SAME_REQUEST
        else:
            attrs = _PLAIN
        lines.append(f'  n{index} [label="{label}" {attrs}];')
    for index in range(len(rows) - 1):
        lines.append(f"  n{index} -> n{index + 1};")
    lines.append("}")
    return "\n".join(lines)
