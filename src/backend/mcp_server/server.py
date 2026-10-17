"""FastMCP server for the chatbot's data and investigation skills.

The server owns the SereneDB connection pool. Every database tool returns the
tool-result envelope defined in ``backend.pipeline.tools.envelope`` (``kind``,
``dataset``, executed ``query``, ``filters``, ``limit``, ``returned``,
``truncated``, ``data`` and, when measured, ``query_stats``), the same contract
as the in-process tools, so the pipeline can switch transports without changing
its evidence contract and an external agent (Claude) can show its work. A query
the layer refuses (unbounded, too wide, unsupported on that corpus) comes back
as ``status: "refused"`` with a ``reason`` and a ``suggestion``, not an error.

Tools take a ``dataset`` id from a closed vocabulary (``backend.db.datasets``),
never a table name, and rows use the canonical field names (``ts``,
``severity``, ``service``, ``entity_id``, ``message``, ``template``,
``event_id``, ``record_id``) whatever the corpus calls them.

New tools must use ``_result`` too; see AGENTS.md "Integrating new tools and tables".
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

from backend.db import (
    compare_periods,
    count_by_field,
    get_field_values,
    get_log_by_record_id,
    get_trend,
    search_logs,
    top_by_field,
)
from backend.db.datasets import DEFAULT_DATASET_ID
from backend.db.stats import QueryResult
from backend.pipeline.sweep import (
    SweepBudget,
    estimate_sweep,
    investigate_chunk,
    sweep,
)
from backend.pipeline.tools.envelope import run_enveloped
from backend.pipeline.tools.schemas import (
    InvestigateChunk,
    SweepChunks,
    ComparePeriods, CountByCategory, GetLogByLineId, GetTools, GetTrend,
    SearchLogs, TopInCategoryByService,
)
from backend.mcp_server.config import MCP_SKILLS_ROOT

mcp = FastMCP("serenedb-logs")


def _validated(model, **values: Any) -> dict[str, Any]:
    return model.model_validate(values).model_dump()


def _result(operation: str, function: Callable[[], Any], *, kind: str | None,
            dataset: str | None = None, filters: dict[str, Any] | None = None,
            limit: int | None = None, complete: bool = False) -> dict[str, Any]:
    """Run ``function`` and return the tool-result envelope (or a refusal).

    ``complete=True`` marks a query with no row cap (not truncated).
    """
    return run_enveloped(operation=operation, function=function, kind=kind,
                         dataset=dataset, filters=filters, limit=limit,
                         complete=complete)


@mcp.tool(name="search_logs_tool")
def search_logs_tool(query_text: str, category: str | None = None,
                     dataset: str = DEFAULT_DATASET_ID, limit: int = 10) -> dict:
    """Search log message content on an indexed corpus, optionally one severity.

    Inputs: query_text (words or a concept; never a severity), category (one of the
    dataset's own severity values, optional), dataset (registered id), limit (1-50,
    default 10). Returns kind=search: at most `limit` example rows (record_id, ts,
    severity, service, entity_id, message, event_id when the corpus has them, and
    relevance) plus the executed query, filters and `truncated`. Examples do not
    measure frequency; use count_by_category_tool.
    """
    v = _validated(SearchLogs, query_text=query_text, category=category,
                   dataset=dataset, limit=limit)
    operation = f"search {v['dataset']} messages for '{v['query_text']}'"
    if v["category"]:
        operation += f" with severity '{v['category']}'"
    return _result(operation, lambda: search_logs(
        v["query_text"], v["dataset"], v["category"], v["limit"]),
        kind="search", dataset=v["dataset"], limit=v["limit"],
        filters={"query_text": v["query_text"], "category": v["category"]})


@mcp.tool(name="get_log_by_line_id_tool")
def get_log_by_line_id_tool(line_id: int, dataset: str = DEFAULT_DATASET_ID) -> dict:
    """Fetch one complete log row by its primary-key id (resolves 'Line ID: n').

    Inputs: line_id (int >= 1), dataset (only corpora with a primary key).
    Returns kind=lookup: zero or one full row (record_id plus the canonical fields).
    """
    v = _validated(GetLogByLineId, line_id=line_id, dataset=dataset)
    return _result(
        f"fetch {v['dataset']} row with id {v['line_id']}",
        lambda: get_log_by_record_id(v["line_id"], v["dataset"]),
        kind="lookup", dataset=v["dataset"], filters={"line_id": v["line_id"]},
        complete=True,
    )


@mcp.tool(name="top_in_category_by_service_tool")
def top_in_category_by_service_tool(start_time: str, end_time: str,
                                    field: str = "service",
                                    dataset: str = DEFAULT_DATASET_ID,
                                    category: str | None = None, limit: int = 5) -> dict:
    """Rank a field's values by log volume in a time window, optionally one severity.

    Inputs: start_time, end_time ('YYYY-MM-DD HH:MM:SS'), field (canonical, default
    service), dataset, category (optional), limit (1-100, default 5). Returns
    kind=aggregate rows {<field>, count} computed over every row in the window;
    `truncated` means more values exist.
    """
    v = _validated(TopInCategoryByService, start_time=start_time, end_time=end_time,
                   field=field, dataset=dataset, category=category, limit=limit)
    return _result(
        f"rank {v['dataset']} {v['field']} by log volume",
        lambda: top_by_field(v["start_time"], v["end_time"], v["field"],
                             v["dataset"], v["category"], v["limit"]),
        kind="aggregate", dataset=v["dataset"], limit=v["limit"],
        filters={"start_time": v["start_time"], "end_time": v["end_time"],
                 "category": v["category"], "group_by": v["field"]},
    )


@mcp.tool(name="compare_periods_tool")
def compare_periods_tool(baseline_start: str, baseline_end: str,
                         recent_start: str, recent_end: str,
                         field: str = "service",
                         dataset: str = DEFAULT_DATASET_ID, limit: int = 5) -> dict:
    """Compare per-value row counts between a baseline and a recent window.

    Inputs: four timestamps ('YYYY-MM-DD HH:MM:SS'), field (canonical, default
    service), dataset, limit (1-100, default 5). Returns kind=aggregate rows
    {<field>, severity, baseline_count, recent_count, volume_change,
    percentage_change}, ordered by volume_change.
    """
    v = _validated(ComparePeriods, baseline_start=baseline_start,
                   baseline_end=baseline_end, recent_start=recent_start,
                   recent_end=recent_end, field=field, dataset=dataset, limit=limit)
    return _result(
        f"compare {v['dataset']} log volume across two time windows",
        lambda: compare_periods(v["baseline_start"], v["baseline_end"],
                                v["recent_start"], v["recent_end"],
                                v["dataset"], v["field"], v["limit"]),
        kind="aggregate", dataset=v["dataset"], limit=v["limit"],
        filters={"baseline_start": v["baseline_start"], "baseline_end": v["baseline_end"],
                 "recent_start": v["recent_start"], "recent_end": v["recent_end"],
                 "group_by": v["field"]},
    )


@mcp.tool(name="count_by_category_tool")
def count_by_category_tool(start_time: str, end_time: str, type: str = "general",
                           dataset: str = DEFAULT_DATASET_ID,
                           category: str | None = None, limit: int = 50) -> dict:
    """Count log rows per severity, overall or broken down by another field.

    Inputs: start_time, end_time ('YYYY-MM-DD HH:MM:SS'), type
    (general|service|entity_id|template|event_id), dataset, category (optional), limit
    (1-200, default 50). Returns kind=aggregate rows {[<type>,] severity, count}
    computed in the database over every matching row: a measured frequency for the
    stated window (row counts, not incidents); `query_stats.approximate` marks a
    corpus without exact counts.
    """
    v = _validated(CountByCategory, start_time=start_time, end_time=end_time,
                   type=type, dataset=dataset, category=category, limit=limit)
    operation = f"count {v['type']} on {v['dataset']} by severity"
    if v["category"]:
        operation += f" for '{v['category']}'"
    group_by = None if v["type"] == "general" else v["type"]
    return _result(operation, lambda: count_by_field(
        v["start_time"], v["end_time"], group_by, v["dataset"],
        v["category"], v["limit"]),
        kind="aggregate", dataset=v["dataset"], limit=v["limit"],
        filters={"start_time": v["start_time"], "end_time": v["end_time"],
                 "category": v["category"],
                 "group_by": "category" if group_by is None else f"{group_by}, category"})


@mcp.tool(name="get_data_tool")
def get_data_tool(start_time: str, end_time: str, type: str,
                  dataset: str = DEFAULT_DATASET_ID, limit: int = 50) -> dict:
    """List the most common values of a field in a time window, with counts.

    Inputs: start_time, end_time, type (canonical field), dataset, limit (1-200,
    default 50). Returns kind=catalog rows {value, count} to discover valid filter
    values; unbounded fields are refused with an alternative.
    """
    v = _validated(GetTools, start_time=start_time, end_time=end_time,
                   type=type, dataset=dataset, limit=limit)
    return _result(
        f"list the most common {v['type']} values on {v['dataset']}",
        lambda: get_field_values(v["start_time"], v["end_time"], v["type"],
                                 v["dataset"], v["limit"]),
        kind="catalog", dataset=v["dataset"], limit=v["limit"],
        filters={"start_time": v["start_time"], "end_time": v["end_time"],
                 "field": v["type"]},
    )


@mcp.tool(name="get_trend_tool")
def get_trend_tool(start_time: str, end_time: str, type: tuple[str, str],
                   dataset: str = DEFAULT_DATASET_ID, bucket: str = "minute",
                   category: str | None = None) -> dict:
    """Count one known field value per time bucket in a window.

    Inputs: start_time < end_time ('YYYY-MM-DD HH:MM:SS'), type ([canonical field,
    exact value]), dataset, bucket (second|minute|hour|day; too many buckets are
    refused with a coarser one suggested), category (optional). Returns kind=trend
    rows {bucket, count, prev_count, trend}.
    """
    v = _validated(GetTrend, start_time=start_time, end_time=end_time, type=type,
                   dataset=dataset, bucket=bucket, category=category)
    field, value = v["type"][0], v["type"][1]
    return _result(
        f"get the {field} trend for '{value}' on {v['dataset']}",
        lambda: get_trend(v["start_time"], v["end_time"], field, value,
                          v["dataset"], v["bucket"], v["category"]),
        kind="trend", dataset=v["dataset"], complete=True,
        filters={"start_time": v["start_time"], "end_time": v["end_time"],
                 field: value, "category": v["category"], "bucket": v["bucket"]},
    )


_SKILLS_ROOT = Path(MCP_SKILLS_ROOT) if MCP_SKILLS_ROOT else Path(__file__).resolve().parents[3] / "skills"


def _skill_files() -> dict[str, Path]:
    if not _SKILLS_ROOT.is_dir():
        raise RuntimeError(f"Skills directory not found: {_SKILLS_ROOT}")
    return {p.parent.name: p for p in _SKILLS_ROOT.glob("*/SKILL.md") if p.is_file()}


def _skill_body(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    return re.sub(r"^---\s*\n.*?\n---\s*\n", "", text, count=1, flags=re.DOTALL)


@mcp.tool(name="sweep_chunks_tool")
def sweep_chunks_tool(question: str, dataset: str = "otel_s3",
                      max_promotions: int = 0, start_at: int = 0,
                      confirm_cost: bool = False) -> dict:
    """Examine a whole remote corpus in graded passes and rank relevant chunks.

    Inputs: question, dataset (a corpus with the chunk-sweep capability),
    max_promotions (0-4; each reads one chunk's message text, minutes each),
    start_at (resume cursor), confirm_cost. Call with confirm_cost=false first: it
    returns a one-row cost estimate rather than running. Returns kind=search rows:
    one {coverage, progress, cursor, notes} row, then {ranked_chunk} and {digest}
    rows. A profiled chunk was counted, not read, so report `coverage`; the
    ranking is not a frequency.
    """
    v = _validated(SweepChunks, question=question, dataset=dataset,
                   max_promotions=max_promotions, start_at=start_at,
                   confirm_cost=confirm_cost)
    budget = SweepBudget(max_promotions=v["max_promotions"])

    if not v["confirm_cost"]:
        estimate = estimate_sweep(v["dataset"], budget=budget)
        estimate["confirm_required"] = True
        return _result(f"estimate the cost of sweeping {v['dataset']}",
                       lambda: QueryResult([estimate], stats=None),
                       kind="search", dataset=v["dataset"], complete=True,
                       filters={"question": v["question"], "confirm_cost": False})

    def run():
        result = sweep(v["question"], dataset=v["dataset"], budget=budget,
                       start_at=v["start_at"])
        payload = result.as_dict()
        rows = [{"coverage": payload["coverage"], "progress": payload["progress"],
                 "cursor": payload["cursor"], "notes": payload["notes"]}]
        rows += [{"ranked_chunk": e} for e in payload["ranked"]]
        rows += [{"digest": d} for d in payload["digests"]]
        return QueryResult(rows, stats=result.stats)

    return _result(f"sweep {v['dataset']} for evidence about {v['question'][:60]!r}",
                   run, kind="search", dataset=v["dataset"], complete=True,
                   filters={"question": v["question"],
                            "max_promotions": v["max_promotions"],
                            "start_at": v["start_at"]})


@mcp.tool(name="investigate_chunk_tool")
def investigate_chunk_tool(chunk_id: str, dataset: str = "otel_s3",
                           question: str = "") -> dict:
    """Read one named chunk's message text and digest it.

    Inputs: chunk_id (from a sweep result), dataset, question (optional). Returns
    kind=search: one row with the chunk's counts, templates, verbatim lines and
    `coverage`. One chunk says nothing about the rest of the corpus.
    """
    v = _validated(InvestigateChunk, chunk_id=chunk_id, dataset=dataset,
                   question=question)
    return _result(
        f"read message text for chunk {v['chunk_id']}",
        lambda: QueryResult(
            [investigate_chunk(v["chunk_id"], question=v["question"],
                               dataset=v["dataset"])],
            stats=None,
        ),
        kind="search", dataset=v["dataset"], complete=True,
        filters={"chunk_id": v["chunk_id"], "question": v["question"]},
    )


@mcp.tool(name="list_skills")
def list_skills() -> list[dict[str, str]]:
    """List investigation skills available to the agent."""
    result = []
    for name, path in _skill_files().items():
        text = path.read_text(encoding="utf-8")
        description = ""
        match = re.search(r"^description:\s*(.+)$", text, re.MULTILINE)
        if match:
            description = match.group(1).strip().strip('"')
        result.append({"name": name, "description": description})
    return result


def _read_skill_text(name: str) -> str:
    if name not in _skill_files() or name in {".", ".."} or "/" in name or "\\" in name:
        raise ValueError("unknown skill")
    return _skill_body(_skill_files()[name])


@mcp.tool(name="read_skill")
def read_skill(name: str) -> str:
    """Read a named investigation skill; names cannot contain paths."""
    return _read_skill_text(name)


@mcp.resource("skill://{name}")
def skill_resource(name: str) -> str:
    """Expose a skill as a read-only MCP resource."""
    return _read_skill_text(name)

# Register concrete URIs as well as the template so clients can discover every
# available skill through ``resources/list`` without guessing names.
for _skill_name in _skill_files():
    def _make_skill_resource(name: str):
        def _resource() -> str:
            return _read_skill_text(name)
        return _resource
    mcp.resource(f"skill://{_skill_name}")(_make_skill_resource(_skill_name))
