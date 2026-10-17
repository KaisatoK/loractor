"""The engineer's before/after timeline, built in code from retrieved rows.

The engineer asks "what happened just before and after?". Ordering lines and
measuring how far each one is from the failing line is bookkeeping, so it is done
here rather than left to the answer model (which would have to sort rows and
subtract timestamps in its head). The output is a derived result record
(``kind="derived"``, ``tool_name="timeline"``) that renders as a table through the
same ``[[RESULT:Rn]]`` marker as every other result and cites the R-ids it was
built from.

Two sources, in order of preference:

1. **A nearby-events result** (``kind="context"``,
   ``get_nearby_events_tool``). Its rows carry an ``offset`` (0 for the anchor),
   so the anchor is read off the rows.
2. **Consecutive lines** (the fallback): ``search`` hits and ``lookup`` rows
   of one dataset whose ``record_id`` values form a run of consecutive ids. The
   anchor is the search hit inside the run (else the middle line), and the offset
   is the exact line distance, because the ids are consecutive.

A timeline is never built across datasets (a record id is only unique within one
corpus), from fewer than two rows, or when no anchor can be identified. It is
row-level context, not a frequency: the record says so
(``measures_frequency=False``) and the answer ledger honours it.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

DERIVED_TOOL_NAME = "timeline"
TIMELINE_FORMULA = (
    "rows ordered by record_id; offset = lines from the anchor line, "
    "seconds = time from the anchor line (negative = before)"
)
# Most rows the timeline shows. The answer prompt shows a derived result up to
# its own cap (``builders.ANSWER_MEASURED_ROWS``); staying at or under it means
# the answer never sees a silently trimmed timeline.
MAX_ROWS = 100
MESSAGE_CHARS = 240

_SOURCE_KINDS = frozenset({"search", "lookup", "context"})
# Names the same field has had over time; the registry's canonical names first.
_ID_KEYS = ("record_id", "line_id")
_TS_KEYS = ("ts", "log_timestamp")
_SEVERITY_KEYS = ("severity", "level", "category")
_SERVICE_KEYS = ("service", "component")
_MESSAGE_KEYS = ("message", "content")


def _first(row: dict, keys: Sequence[str]) -> Any:
    return next((row[k] for k in keys if row.get(k) is not None), None)


def _record_id(row: dict) -> int | None:
    value = _first(row, _ID_KEYS)
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_ts(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _seconds(anchor: datetime | None, other: datetime | None) -> float | None:
    if anchor is None or other is None:
        return None
    try:
        return round((other - anchor).total_seconds(), 3)
    except TypeError:  # one side has a UTC offset and the other does not
        return None


def _clip(value: Any) -> str:
    text = str(value)
    return text if len(text) <= MESSAGE_CHARS else text[:MESSAGE_CHARS] + "…[cut]"


def _usable(record: Any) -> bool:
    return (
        isinstance(record, dict)
        and record.get("kind") in _SOURCE_KINDS
        and bool(record.get("rows"))
        and record.get("tool_name") != DERIVED_TOOL_NAME
    )


def _candidates(records: Sequence[dict]) -> list[tuple[dict, dict, str]]:
    """``(row, record, dataset)`` for every row that can sit on a timeline."""
    found = []
    for record in records:
        if not _usable(record):
            continue
        dataset = record.get("dataset") or ""
        for row in record["rows"]:
            if isinstance(row, dict) and _record_id(row) is not None and _first(
                row, _TS_KEYS
            ) is not None:
                found.append((row, record, dataset))
    return found


def _from_context(records: Sequence[dict]) -> tuple[list[dict], dict, dict] | None:
    """The first nearby-events result that contains its own anchor row."""
    for record in records:
        if not (_usable(record) and record.get("kind") == "context"):
            continue
        rows = [
            r for r in record["rows"]
            if isinstance(r, dict) and _record_id(r) is not None
            and _first(r, _TS_KEYS) is not None
        ]
        anchor = next((r for r in rows if r.get("offset") == 0), None)
        if anchor is not None:
            return rows, anchor, record
    return None


def _runs(ids: list[int]) -> list[list[int]]:
    """Split sorted ids into runs of consecutive values."""
    runs: list[list[int]] = []
    for rid in ids:
        if runs and rid == runs[-1][-1] + 1:
            runs[-1].append(rid)
        else:
            runs.append([rid])
    return runs


def _longest_run(
    candidates: list[tuple[dict, dict, str]],
) -> list[tuple[dict, dict]] | None:
    """The longest run of consecutive record ids within one dataset.

    Ties go to the run that appears first in the tool results. Duplicate ids (the
    same line returned by a search and a lookup) keep their first occurrence.
    """
    by_dataset: dict[str, dict[int, tuple[int, dict, dict]]] = {}
    for position, (row, record, dataset) in enumerate(candidates):
        by_dataset.setdefault(dataset, {}).setdefault(
            _record_id(row), (position, row, record)
        )

    def rank(members: list[tuple[int, dict, dict]]) -> tuple[int, int]:
        # Longer first; among equals, the one seen earliest.
        return (len(members), -min((m[0] for m in members), default=0))

    best: list[tuple[int, dict, dict]] = []
    for rows_by_id in by_dataset.values():
        for run in _runs(sorted(rows_by_id)):
            members = [rows_by_id[rid] for rid in run]
            if rank(members) > rank(best):
                best = members
    return [(row, record) for _, row, record in best] if len(best) >= 2 else None


def _anchor_of_run(run: list[tuple[dict, dict]]) -> dict:
    """The search hit inside the run, else the middle line (lower middle)."""
    for row, record in run:
        if record.get("kind") == "search":
            return row
    return run[(len(run) - 1) // 2][0]


def build_timeline(records: Sequence[dict]) -> dict[str, Any] | None:
    """Ordered rows around the anchor line, or ``None`` when none can be built."""
    records = [r for r in records if isinstance(r, dict)]
    context = _from_context(records)
    if context is not None:
        rows, anchor, source = context
        dataset = source.get("dataset") or ""
        offsets = {id(r): r.get("offset") for r in rows}
        sources = [source["id"]] if source.get("id") else []
        source_of = {id(r): source.get("id") for r in rows}
    else:
        run = _longest_run(_candidates(records))
        if run is None:
            return None
        anchor = _anchor_of_run(run)
        rows = [row for row, _ in run]
        anchor_id = _record_id(anchor)
        offsets = {id(row): _record_id(row) - anchor_id for row in rows}
        source_of = {id(row): record.get("id") for row, record in run}
        dataset = run[0][1].get("dataset") or ""
        sources = sorted(
            {record.get("id") for _, record in run if record.get("id")},
            key=lambda i: (len(i), i),
        )

    if len(rows) < 2:
        return None

    anchor_ts = _parse_ts(_first(anchor, _TS_KEYS))
    anchor_request = anchor.get("request_id")
    ordered = sorted(rows, key=_record_id)
    capped = len(ordered) > MAX_ROWS
    if capped:
        # Keep the lines nearest the anchor, then restore line order.
        ordered = sorted(
            sorted(ordered, key=lambda r: abs(offsets[id(r)] or 0))[:MAX_ROWS],
            key=_record_id,
        )

    out = []
    for row in ordered:
        item = {
            "offset": offsets[id(row)],
            "seconds": _seconds(anchor_ts, _parse_ts(_first(row, _TS_KEYS))),
            "record_id": _record_id(row),
            "ts": str(_first(row, _TS_KEYS)),
            "severity": _first(row, _SEVERITY_KEYS),
            "service": _first(row, _SERVICE_KEYS),
            "message": _clip(_first(row, _MESSAGE_KEYS) or ""),
        }
        if "request_id" in row and anchor_request is not None:
            item["same_request"] = row.get("request_id") == anchor_request
        item["from"] = source_of.get(id(row))
        out.append(item)

    return {
        "dataset": dataset or None,
        "anchor_record_id": _record_id(anchor),
        "rows": out,
        "source_ids": [s for s in sources if s],
        "total_rows": len(rows),
        "capped": capped,
    }


def timeline_record(records: Sequence[dict]) -> dict[str, Any] | None:
    """The derived ``R{n}`` record for the timeline, or ``None`` without one."""
    base = [r for r in records if r.get("tool_name") != DERIVED_TOOL_NAME]
    timeline = build_timeline(base)
    if timeline is None:
        return None
    rows = timeline["rows"]
    sources = timeline["source_ids"]
    return {
        "id": f"R{len(base) + 1}",
        "tool_name": DERIVED_TOOL_NAME,
        "arguments": {
            "dataset": timeline["dataset"],
            "anchor_record_id": timeline["anchor_record_id"],
            "from": sources,
        },
        "row_count": len(rows),
        "rows": rows,
        "rows_capped": timeline["capped"],
        "kind": "derived",
        "dataset": timeline["dataset"],
        "query": None,
        "filters": {"anchor_record_id": timeline["anchor_record_id"]},
        "limit": MAX_ROWS,
        "returned": len(rows),
        "total_matches": timeline["total_rows"],
        "truncated": timeline["capped"],
        "formula": TIMELINE_FORMULA,
        "source_ids": sources,
        # Row-level context around one line. The answer ledger must not present
        # it as a frequency, even though a derived result is otherwise computed.
        "measures_frequency": False,
    }


def with_timeline(records: Sequence[dict]) -> list[dict]:
    """Records with exactly one fresh derived timeline record (idempotent)."""
    base = [r for r in records if r.get("tool_name") != DERIVED_TOOL_NAME]
    derived = timeline_record(base)
    return [*base, derived] if derived else base
