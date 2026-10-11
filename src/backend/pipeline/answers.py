"""Serialize structured answer blocks into the marker text the UI renders.

The answer agent returns an :class:`~backend.pipeline.contracts.AnswerOutput`
(ordered prose and table blocks). The UI's rendering protocol, however, consumes
a plain string in which a table is a ``[[RESULT:Rx]]`` marker alone on its own
line. This module is the single place that turns the one into the other, so the
marker text is always machine-generated and well formed.
"""

from __future__ import annotations

import re
from typing import Any

from backend.pipeline.contracts import AnswerTable, AnswerText

# Canonical, well-formed marker written for each table block.
MARKER_TEMPLATE = "[[RESULT:{result_id}]]"

# Tolerant matcher for marker-like text the model may still echo inside a prose
# block (mirrors the UI's MARKER_RE). Such text is stripped so a stray marker can
# never reach the renderer from a prose block.
_MARKER_RE = re.compile(
    r"[\[【［]{1,2}\s*(?:RESULT\s*:\s*)?(R\d+)\s*[\]】］]{1,2}"
)


def serialize_answer(answer: Any, valid_ids: set[str]) -> str:
    """Render structured answer blocks as the marker text the UI expects.

    Text blocks become prose; table blocks become a ``[[RESULT:Rx]]`` marker on
    its own line. Table blocks whose id is absent from ``valid_ids`` are dropped
    and repeated ids are collapsed. The model decides how many tables to show.
    """
    blocks = _read_blocks(answer)
    pieces: list[str] = []
    seen_tables: set[str] = set()

    for block in blocks:
        kind, text, result_id = _read_block(block)
        if kind == "table":
            result_id = (result_id or "").strip()
            if result_id in valid_ids and result_id not in seen_tables:
                seen_tables.add(result_id)
                pieces.append(MARKER_TEMPLATE.format(result_id=result_id))
        else:
            prose = re.sub(r"[ \t]{2,}", " ", _MARKER_RE.sub("", text or "")).strip()
            if prose:
                pieces.append(prose)

    return "\n\n".join(pieces)


def _read_blocks(answer: Any) -> list:
    """Return the block list from a pydantic ``AnswerOutput`` or a plain dict."""
    if isinstance(answer, dict):
        return answer.get("blocks") or []
    return getattr(answer, "blocks", None) or []


def _read_block(block: Any) -> tuple[str, str, str]:
    """Return ``(kind, text, result_id)`` for a pydantic or dict block."""
    if isinstance(block, AnswerTable):
        return "table", "", block.result_id
    if isinstance(block, AnswerText):
        return "text", block.text, ""
    if isinstance(block, dict):
        if block.get("type") == "table" or block.get("result_id"):
            return "table", "", block.get("result_id", "")
        return "text", block.get("text", ""), ""
    if getattr(block, "result_id", None):
        return "table", "", block.result_id
    return "text", getattr(block, "text", ""), ""
