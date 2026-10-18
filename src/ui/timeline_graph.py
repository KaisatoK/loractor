"""Mermaid flowchart for the engineer's before -> anchor -> after chain.

Built from the derived ``timeline`` record (``backend.pipeline.timeline``), never by
a model: the answer still only cites ``[[RESULT:Rn]]`` and the table stays the
source of truth. This module is pure text-in, text-out (stdlib only, no Streamlit,
no backend import), so the diagram is testable without a runtime.

The chart is laid out left to right with two short lines per node so it stays
compact in the chat: the chain wraps into rows of ``NODES_PER_ROW`` boxes
(``st.mermaid_chart`` fits it to the chat width) and shows a window of at most
``MAX_NODES`` lines around the anchor.

Log text is untrusted, so no message text is drawn at all: a node shows only the
offset, the seconds from the anchor, the severity and the service. Each of those is
reduced to a small safe character set; everything else becomes a Mermaid numeric
entity (``#60;``), so a value can never close the label or add syntax. Node ids are
generated (``n0``, ``n1``...), never taken from the data.
"""

from __future__ import annotations

from typing import Any

TIMELINE_TOOL_NAME = "timeline"
# Most nodes drawn: a window of lines centred on the anchor (the table lists them all).
MAX_NODES = 12
# Boxes per row; the chain wraps into a snake-like grid so each box stays readable.
NODES_PER_ROW = 4
_LABEL_CHARS = 28

# Punctuation that is harmless inside a quoted Mermaid label (letters and digits are
# always kept). Everything else, including quotes, brackets, `<`, `>`, `&`, `#` and
# `;`, is written as an entity.
_SAFE_PUNCTUATION = frozenset(" _.-/:()@+?…,")

# Mermaid's default block padding leaves no room to draw an arrow between boxes, so
# the diagram carries its own config (st.mermaid_chart takes none).
_FRONTMATTER = ("---", "config:", "  block:", "    padding: 22", "---")

_CLASS_DEFS = (
    "  classDef anchor fill:#ffd6d6,stroke:#c0392b,stroke-width:2px,color:#222;",
    "  classDef request fill:#e3f0ff,stroke:#2e6da4,color:#222;",
    "  classDef plain fill:#f5f5f5,stroke:#999999,color:#222;",
)


def _escape(value: Any) -> str:
    """Make ``value`` safe inside a double-quoted Mermaid label.

    Control characters (newlines included) become spaces, the text is cut so one
    long service name cannot widen the chart, and any character outside the safe set
    is written as a numeric entity.
    """
    text = "" if value is None else str(value)
    text = "".join(" " if ord(ch) < 32 or ord(ch) == 127 else ch for ch in text)
    if len(text) > _LABEL_CHARS:
        text = text[: _LABEL_CHARS - 1] + "…"
    return "".join(
        ch if ch.isalnum() or ch in _SAFE_PUNCTUATION else f"#{ord(ch)};" for ch in text
    )


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


def timeline_mermaid(record: dict) -> str | None:
    """Mermaid text for a ``timeline`` record, or ``None`` when there is nothing to draw.

    Nodes follow record-id order (the table's order), at most ``MAX_NODES`` of them,
    a window centred on the anchor, drawn as a Mermaid block diagram (a real grid).
    The chain snakes: the first row reads left to right, the next right to left, and
    so on, so the last box of a row sits directly above the first of the next and
    every arrow, including the turn between rows, is drawn. The anchor is highlighted
    and rows on the anchor's request are marked.
    """
    if not isinstance(record, dict) or record.get("tool_name") != TIMELINE_TOOL_NAME:
        return None
    rows = [r for r in (record.get("rows") or []) if isinstance(r, dict)]
    if len(rows) < 2:
        return None
    rows = sorted(rows, key=_record_id)
    arguments = record.get("arguments")
    anchor_id = arguments.get("anchor_record_id") if isinstance(arguments, dict) else None
    if len(rows) > MAX_NODES:
        anchor_index = next((i for i, r in enumerate(rows) if _is_anchor(r, anchor_id)), 0)
        start = max(0, min(anchor_index - MAX_NODES // 2, len(rows) - MAX_NODES))
        rows = rows[start : start + MAX_NODES]

    labels: list[tuple[str, str]] = []
    for row in rows:
        offset = _escape(_signed(row.get("offset")))
        seconds = _escape(_signed(row.get("seconds"), suffix="s"))
        label = (
            f"{offset} · {seconds}<br/>"
            f"{_escape(row.get('severity'))} · {_escape(row.get('service'))}"
        )
        if _is_anchor(row, anchor_id):
            labels.append(("anchor", f"ANCHOR {label}"))
        elif row.get("same_request") is True:
            labels.append(("request", label))
        else:
            labels.append(("plain", label))

    lines = [*_FRONTMATTER, "block-beta", f"  columns {NODES_PER_ROW}"]
    for first in range(0, len(rows), NODES_PER_ROW):
        band = list(range(first, min(first + NODES_PER_ROW, len(rows))))
        if (first // NODES_PER_ROW) % 2 == 1:
            # Odd rows run right to left; a short last row is padded on the side
            # the chain arrives from, so it stays under the previous row's end.
            band = band[::-1]
            band = [None] * (NODES_PER_ROW - len(band)) + band
        lines.append(
            "  " + " ".join(f'n{i}["{labels[i][1]}"]' if i is not None else "space" for i in band)
        )
    lines.extend(f"  n{i} --> n{i + 1}" for i in range(len(rows) - 1))
    lines.extend(f"  class n{i} {css}" for i, (css, _) in enumerate(labels))
    lines.extend(_CLASS_DEFS)
    return "\n".join(lines)
