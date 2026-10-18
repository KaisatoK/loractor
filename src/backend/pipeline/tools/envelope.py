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

Every new tool must return
this shape; ``collect_tool_results`` tolerates legacy envelopes by filling
missing fields with ``None`` (inferring only ``kind`` from known tool names), and
the answer reports those fields as unknown rather than guessing.
"""

import datetime as dt
import json
import uuid
from collections.abc import Callable
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from backend.db.capture import capture_queries
from backend.pipeline.tools.specs import SPECS

# ``search``    limited set of relevant log lines (examples, never a frequency)
# ``context``   events around an anchor line (nearby events / timeline)
# ``aggregate`` counts grouped in the database (whole scope, a measured number)
# ``trend``     counts per time bucket
# ``lookup``    one record by key
# ``catalog``   discovery: a field's known values, or each dataset's rows/span/levels
# ``derived``   computed in code from measured counts (the priority ranking)
# ``test``      reserved; no tool returns it (the hypothesis-test tools were dropped)
RESULT_KINDS = (
    "search", "context", "aggregate", "trend", "lookup", "catalog", "derived", "test"
)

# Kinds whose numbers measure frequency over the stated scope.
MEASURED_KINDS = frozenset({"aggregate", "trend"})

# Kind of every declared tool, read from the spec table (``tools/specs.py``).
# Legacy envelopes that omit ``kind`` are classified from it, and a tool name in
# it is what makes a message a database result rather than a skill read.
TOOL_KINDS: dict[str, str] = {spec.name: spec.kind for spec in SPECS}

# --- Size limits ---------------------------------------------------------------
# What one tool result may put into a model's context. Every transport applies
# them in ``build_envelope`` so the local and MCP tools return identical rows,
# and a cut is always visible: a shortened cell ends in CUT_MARKER (a silent cut
# once made the answer quote a shortened hash as if it were the real value) and
# the envelope counts what was cut.
CUT_MARKER = "…[cut]"
MAX_CELL_CHARS = 2_000
MAX_RESULT_CHARS = 200_000
# A lookup returns "one complete row" by contract -- it is how a cut search cell
# is read in full -- so it is never clipped.
UNCLIPPED_KINDS = frozenset({"lookup"})


def clip_rows(
    rows: list[Any],
    *,
    max_cell: int = MAX_CELL_CHARS,
    max_total: int = MAX_RESULT_CHARS,
) -> tuple[list[Any], int, int]:
    """``(rows, clipped_cells, dropped_rows)`` within the size limits.

    Text longer than ``max_cell`` characters is cut (also inside nested cells),
    then trailing rows are dropped until the serialized rows fit ``max_total``
    characters. The first row is always kept, so a result is never emptied.
    """
    clipped = 0

    def clip(value: Any) -> Any:
        nonlocal clipped
        if isinstance(value, str):
            if len(value) > max_cell:
                clipped += 1
                return value[:max_cell] + CUT_MARKER
            return value
        if isinstance(value, dict):
            return {k: clip(v) for k, v in value.items()}
        if isinstance(value, list):
            return [clip(v) for v in value]
        return value

    rows = [clip(row) for row in rows]
    kept, size = 0, 0
    for row in rows:
        size += len(json.dumps(row, default=str))
        if kept and size > max_total:
            break
        kept += 1
    return rows[:kept], clipped, len(rows) - kept


class ToolEnvelope(BaseModel):
    """The result of every evidence tool: what the server advertises as its output.

    Success and refusal share one model because a refusal is a result, not an
    error. ``build_envelope`` and ``refusal.as_tool_result`` produce the dicts;
    this model documents them and is what MCP clients validate against, so it
    must describe exactly what they return.
    """

    model_config = ConfigDict(extra="allow")

    status: str = Field(
        description=(
            "'success', or 'refused': the request was not run because it was "
            "unbounded, too wide or unsupported; `reason` says why and "
            "`suggestion` is a narrower request that would be accepted."
        )
    )
    operation: str | None = Field(None, description="What the call did, in words.")
    kind: str | None = Field(
        None,
        description=(
            "What the rows are. 'aggregate' and 'trend' are measured frequencies "
            "over the stated scope; 'derived' rows are computed in code from the "
            "measured counts whose queries are listed in `query`; 'search', "
            "'context', 'lookup' and 'catalog' rows are examples or metadata and "
            "never establish how often something happens."
        ),
    )
    dataset: str | None = Field(None, description="The queried corpus id.")
    query: str | None = Field(
        None, description="The SQL that ran, parameters bound. Cite it for every number."
    )
    filters: dict[str, Any] | None = Field(
        None, description="The filters that were applied (window, severity, field)."
    )
    limit: int | None = Field(None, description="The row cap the tool applied, if any.")
    returned: int | None = Field(None, description="Rows in `data`.")
    total_matches: int | None = Field(
        None, description="Every row matching the filters, when the tool counted them."
    )
    truncated: bool | None = Field(
        None,
        description=(
            "True when more matching rows exist than were returned, False when "
            "the result is provably complete, null when unknown."
        ),
    )
    data: list[dict[str, Any]] | None = Field(None, description="The result rows.")
    query_stats: dict[str, Any] | None = Field(
        None,
        description=(
            "What the query cost: rows scanned, time, files read, whether the "
            "counts are approximate, and notes."
        ),
    )
    clipped_cells: int | None = Field(
        None,
        description=(
            f"Text cells cut to {MAX_CELL_CHARS} characters (they end in "
            f"'{CUT_MARKER}'). Fetch the full line with get_log_by_line_id_tool."
        ),
    )
    clipped_rows: int | None = Field(
        None, description="Rows dropped to keep the result within the size limit."
    )
    code: str | None = Field(None, description="Refusals: a stable category.")
    reason: str | None = Field(None, description="Refusals: what was wrong.")
    suggestion: str | None = Field(
        None, description="Refusals: a concrete request that would be accepted."
    )


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
    clipped_cells = dropped_rows = 0
    if kind not in UNCLIPPED_KINDS:
        rows, clipped_cells, dropped_rows = clip_rows(rows)
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
    if dropped_rows:
        # Rows were dropped for size, so the result is incomplete whatever the
        # query's own cap says.
        envelope["truncated"] = True
        envelope["clipped_rows"] = dropped_rows
    if clipped_cells:
        envelope["clipped_cells"] = clipped_cells
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
        for key in ("clipped_cells", "clipped_rows"):
            if remote.get(key):
                envelope[key] = remote[key]
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
