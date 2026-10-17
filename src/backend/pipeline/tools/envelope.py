"""The tool-result contract shared by local tools, the MCP server and the answer.

Every evidence tool returns one envelope::

    {
      "status": "success",
      "operation": "search log messages for 'timeout'",
      "kind": "search",            # see TOOL_KINDS / RESULT_KINDS
      "dataset": "openstack",      # registry id of the queried corpus
      "query": "SELECT ...",       # executed SQL with parameters bound
      "filters": {"query_text": "timeout", "category": "ERROR"},
      "limit": 10,                 # row cap the tool applied, or None
      "returned": 10,              # rows in ``data``
      "total_matches": None,       # full match count when the tool knows it
      "truncated": True,           # True / False / None (unknown)
      "data": [...],
    }

``truncated`` is what lets an answer say "these are examples, not a count":
``True`` when ``total_matches`` exceeds ``returned`` or the result hit
``limit``; ``False`` when it provably did not; ``None`` when neither is known.

A database function may return a ``backend.db.stats.QueryResult`` (a ``list`` of
rows carrying ``.stats`` and ``.refusal``). Its cost record is copied into the
envelope as ``query_stats``, and a refused query yields the refusal payload
(``status: "refused"``, ``reason``, ``suggestion``) instead of an envelope, so
the agent narrows and retries rather than seeing an error.

Future tools (team counts, hypothesis tests) must return
this shape; ``collect_tool_results`` tolerates legacy envelopes by filling
missing fields with ``None`` (inferring only ``kind`` from known tool names), and
the answer reports those fields as unknown rather than guessing.
"""

import datetime as dt
import uuid
from collections.abc import Callable
from decimal import Decimal
from typing import Any

from backend.db.capture import capture_queries

# ``search``    limited set of relevant log lines (examples, never a frequency)
# ``context``   events around an anchor line (nearby events / timeline)
# ``aggregate`` counts grouped in the database (whole scope, a measured number)
# ``trend``     counts per time bucket
# ``lookup``    one record by key
# ``catalog``   discovery: a field's known values, or each dataset's rows/span/levels
# ``test``      a deterministic hypothesis test (planned; see AGENTS.md)
RESULT_KINDS = ("search", "context", "aggregate", "trend", "lookup", "catalog", "test")

# Kinds whose numbers measure frequency over the stated scope.
MEASURED_KINDS = frozenset({"aggregate", "trend"})

# Kind of every tool name currently known. Legacy envelopes that omit ``kind``
# are classified from this table; add each new tool here.
TOOL_KINDS: dict[str, str] = {
    # One row per dataset: rows, span and levels, for choosing a window.
    "list_datasets_tool": "catalog",
    "search_logs_tool": "search",
    "get_log_by_line_id_tool": "lookup",
    "get_data_tool": "catalog",
    "count_by_category_tool": "aggregate",
    "count_by_dataset_tool": "aggregate",
    "top_in_category_by_service_tool": "aggregate",
    "compare_periods_tool": "aggregate",
    "get_trend_tool": "trend",
    # Lines around one anchor line: examples for a timeline, never a frequency.
    "get_nearby_events_tool": "context",
    # Implemented team counts: database aggregate joined to ownership table.
    "count_by_team_tool": "aggregate",
    # The sweep ranks chunks from profiles and samples and digests the ones it
    # reads: examples and coverage, never a frequency over the corpus.
    "sweep_chunks_tool": "search",
    "investigate_chunk_tool": "search",
}

ENVELOPE_FIELDS = (
    "kind",
    "dataset",
    "query",
    "filters",
    "limit",
    "returned",
    "total_matches",
    "truncated",
)


def json_safe(value: Any) -> Any:
    """Rows as plain JSON types.

    Database rows carry ``datetime`` and ``Decimal`` values (a search's
    ``log_timestamp`` and ``relevance``). LangChain serializes a tool result with
    ``json.dumps`` and silently falls back to a Python ``repr`` when that fails,
    which made every such result unparseable downstream and dropped it from the
    captured results. Convert at the source instead.
    """
    if isinstance(value, dict):
        return {k: json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    if isinstance(value, dt.datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, (dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value) if value.is_finite() else str(value)
    if isinstance(value, (uuid.UUID, bytes)):
        return str(value)
    return value


def is_truncated(
    returned: int,
    limit: int | None,
    total_matches: int | None,
    complete: bool = False,
) -> bool | None:
    """Whether more matching rows exist than were returned (``None`` = unknown).

    ``complete`` is set by tools whose query has no row cap (grouped counts,
    per-minute trends, value lists, key lookups): every matching group is
    returned, so the result is not truncated.
    """
    if total_matches is not None:
        return total_matches > returned
    if limit is not None:
        # Hitting the cap means more rows may exist; staying below it proves
        # the result is complete.
        return returned >= limit
    return False if complete else None


def build_envelope(
    *,
    operation: str,
    data: Any,
    kind: str | None,
    dataset: str | None = None,
    filters: dict[str, Any] | None = None,
    limit: int | None = None,
    queries: list[str] | None = None,
    total_matches: int | None = None,
    complete: bool = False,
    query_stats: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble the success envelope for one tool result."""
    rows = list(data) if isinstance(data, list) else ([] if data is None else [data])
    rows = json_safe(rows)
    returned = len(rows)
    envelope = {
        "status": "success",
        "operation": operation,
        "kind": kind,
        "dataset": dataset,
        "query": ";\n".join(queries) if queries else None,
        "filters": {k: v for k, v in (filters or {}).items() if v is not None},
        "limit": limit,
        "returned": returned,
        "total_matches": total_matches,
        "truncated": is_truncated(returned, limit, total_matches, complete),
        "data": rows,
    }
    if query_stats:
        envelope["query_stats"] = json_safe(query_stats)
    return envelope


def refusal_of(data: Any) -> Any:
    """The refusal of a ``QueryResult`` that was refused, else ``None``."""
    return getattr(data, "refusal", None) if getattr(data, "refused", False) else None


def _stats_of(data: Any) -> dict[str, Any] | None:
    stats = getattr(data, "stats", None)
    return stats.as_dict() if stats is not None else None


def run_enveloped(
    *,
    operation: str,
    function: Callable[[], Any],
    kind: str | None,
    dataset: str | None = None,
    filters: dict[str, Any] | None = None,
    limit: int | None = None,
    complete: bool = False,
) -> dict[str, Any]:
    """Run a query function and wrap its rows plus the executed SQL.

    If ``function`` already returns an envelope (an MCP payload), its metadata
    wins over the locally declared values so the server stays authoritative. A
    refused query (raised ``QueryRefused`` or a refused ``QueryResult``) and a
    remote refusal payload are returned as the refusal payload, never wrapped.
    """
    from backend.db.errors import QueryRefused  # lazy: keeps the import graph flat

    with capture_queries() as queries:
        try:
            data = function()
        except QueryRefused as refused:
            return {"operation": operation, **refused.refusal.as_tool_result()}
    refusal = refusal_of(data)
    if refusal is not None:
        return {"operation": operation, **refusal.as_tool_result()}
    if isinstance(data, dict) and data.get("status") == "refused":
        return {"operation": data.get("operation") or operation, **data}
    if isinstance(data, dict) and "data" in data and "status" in data:
        remote = data
        envelope = build_envelope(
            operation=remote.get("operation") or operation,
            data=remote.get("data"),
            kind=remote.get("kind") or kind,
            dataset=remote.get("dataset"),
            filters=remote.get("filters") if remote.get("filters") is not None else filters,
            limit=remote.get("limit", limit),
            queries=[remote["query"]] if remote.get("query") else queries,
            total_matches=remote.get("total_matches"),
            complete=complete,
            query_stats=remote.get("query_stats"),
        )
        if "truncated" in remote and remote["truncated"] is not None:
            envelope["truncated"] = remote["truncated"]
        return envelope
    return build_envelope(
        operation=operation,
        data=data,
        kind=kind,
        dataset=dataset,
        filters=filters,
        limit=limit,
        queries=queries,
        total_matches=getattr(data, "total_matches", None),
        complete=complete,
        query_stats=_stats_of(data),
    )


def envelope_metadata(payload: dict[str, Any], tool_name: str | None) -> dict[str, Any]:
    """The contract fields of a payload; missing ones become ``None``.

    Only ``kind`` is inferred (from ``TOOL_KINDS``) because it is a property of
    the tool, not of the call. Nothing else is guessed.
    """
    meta = {field: payload.get(field) for field in ENVELOPE_FIELDS}
    if meta["kind"] is None:
        meta["kind"] = TOOL_KINDS.get(tool_name or "")
    return meta
