"""FastMCP server for the chatbot's data and investigation skills.

The server owns the SereneDB connection pool. It deliberately returns the same
small ``status/operation/data`` envelope used by the in-process tools so the
pipeline can switch transports without changing its evidence contract.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

from backend.db import (
    auto_complete,
    compare_periods,
    count_by_field,
    get_field_values,
    get_log_by_record_id,
    get_trend,
    search_logs,
    top_by_field,
)
from backend.db.datasets import DEFAULT_DATASET_ID
from backend.db.errors import QueryRefused
from backend.db.stats import QueryResult
from backend.pipeline.sweep import (
    SweepBudget,
    estimate_sweep,
    investigate_chunk,
    sweep,
)
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


def _result(operation: str, data: Any) -> dict[str, Any]:
    """The same envelope the in-process tools return, including cost.

    ``query_stats`` rides along so an MCP client gets the same cost accounting
    as the local transport, and a refusal is reported as a refusal rather than
    raised -- an external agent needs the suggestion just as much as ours does.
    """
    payload: dict[str, Any] = {
        "status": "success",
        "operation": operation,
        "data": list(data),
    }
    stats = getattr(data, "stats", None)
    if stats is not None:
        payload["query_stats"] = stats.as_dict()
    return payload


def _run(operation: str, function) -> dict[str, Any]:
    try:
        return _result(operation, function())
    except QueryRefused as refused:
        return {"operation": operation, **refused.refusal.as_tool_result()}


@mcp.tool(name="search_logs_tool")
def search_logs_tool(query_text: str, category: str | None = None,
                     dataset: str = DEFAULT_DATASET_ID, limit: int = 10) -> dict:
    """Search log message content on an indexed corpus."""
    v = _validated(SearchLogs, query_text=query_text, category=category,
                   dataset=dataset, limit=limit)
    operation = f"search {v['dataset']} messages for '{v['query_text']}'"
    if v["category"]:
        operation += f" with severity '{v['category']}'"
    return _run(operation, lambda: search_logs(
        v["query_text"], v["dataset"], v["category"], v["limit"]))


@mcp.tool(name="get_log_by_line_id_tool")
def get_log_by_line_id_tool(line_id: int, dataset: str = DEFAULT_DATASET_ID) -> dict:
    """Fetch one complete log row by its primary-key id."""
    v = _validated(GetLogByLineId, line_id=line_id, dataset=dataset)
    return _run(
        f"fetch {v['dataset']} row with id {v['line_id']}",
        lambda: get_log_by_record_id(v["line_id"], v["dataset"]),
    )


@mcp.tool(name="top_in_category_by_service_tool")
def top_in_category_by_service_tool(start_time: str, end_time: str,
                                    field: str = "service",
                                    dataset: str = DEFAULT_DATASET_ID,
                                    category: str | None = None, limit: int = 5) -> dict:
    """Rank a field's values by log volume in a time window."""
    v = _validated(TopInCategoryByService, start_time=start_time, end_time=end_time,
                   field=field, dataset=dataset, category=category, limit=limit)
    return _run(
        f"rank {v['dataset']} {v['field']} by log volume",
        lambda: top_by_field(v["start_time"], v["end_time"], v["field"],
                             v["dataset"], v["category"], v["limit"]),
    )


@mcp.tool(name="compare_periods_tool")
def compare_periods_tool(baseline_start: str, baseline_end: str,
                         recent_start: str, recent_end: str,
                         field: str = "service",
                         dataset: str = DEFAULT_DATASET_ID, limit: int = 5) -> dict:
    """Compare counts between a baseline and a recent window."""
    v = _validated(ComparePeriods, baseline_start=baseline_start,
                   baseline_end=baseline_end, recent_start=recent_start,
                   recent_end=recent_end, field=field, dataset=dataset, limit=limit)
    return _run(
        f"compare {v['dataset']} log volume across two time windows",
        lambda: compare_periods(v["baseline_start"], v["baseline_end"],
                                v["recent_start"], v["recent_end"],
                                v["dataset"], v["field"], v["limit"]),
    )


@mcp.tool(name="count_by_category_tool")
def count_by_category_tool(start_time: str, end_time: str, type: str = "general",
                           dataset: str = DEFAULT_DATASET_ID,
                           category: str | None = None, limit: int = 50) -> dict:
    """Count log rows by severity, optionally broken down by another field."""
    v = _validated(CountByCategory, start_time=start_time, end_time=end_time,
                   type=type, dataset=dataset, category=category, limit=limit)
    operation = f"count {v['type']} on {v['dataset']} by severity"
    if v["category"]:
        operation += f" for '{v['category']}'"
    group_by = None if v["type"] == "general" else v["type"]
    return _run(operation, lambda: count_by_field(
        v["start_time"], v["end_time"], group_by, v["dataset"],
        v["category"], v["limit"]))


@mcp.tool(name="get_data_tool")
def get_data_tool(start_time: str, end_time: str, type: str,
                  dataset: str = DEFAULT_DATASET_ID, limit: int = 50) -> dict:
    """List the most common values of a field in a time window, with counts."""
    v = _validated(GetTools, start_time=start_time, end_time=end_time,
                   type=type, dataset=dataset, limit=limit)
    return _run(
        f"list the most common {v['type']} values on {v['dataset']}",
        lambda: get_field_values(v["start_time"], v["end_time"], v["type"],
                                 v["dataset"], v["limit"]),
    )


@mcp.tool(name="get_trend_tool")
def get_trend_tool(start_time: str, end_time: str, type: tuple[str, str],
                   dataset: str = DEFAULT_DATASET_ID, bucket: str = "minute",
                   category: str | None = None) -> dict:
    """Count one known field value per time bucket."""
    v = _validated(GetTrend, start_time=start_time, end_time=end_time, type=type,
                   dataset=dataset, bucket=bucket, category=category)
    field, value = v["type"][0], v["type"][1]
    return _run(
        f"get the {field} trend for '{value}' on {v['dataset']}",
        lambda: get_trend(v["start_time"], v["end_time"], field, value,
                          v["dataset"], v["bucket"], v["category"]),
    )


@mcp.tool(name="auto_complete")
def auto_complete_tool(query: str, dataset: str = DEFAULT_DATASET_ID) -> dict:
    """Suggest indexed log values for a selector-prefixed query."""
    return _run(f"auto-complete '{query}'", lambda: auto_complete(query, dataset))


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

    Call with confirm_cost=false first: it returns a cost estimate rather than
    running, because a sweep with promotions costs minutes per chunk.
    """
    v = _validated(SweepChunks, question=question, dataset=dataset,
                   max_promotions=max_promotions, start_at=start_at,
                   confirm_cost=confirm_cost)
    budget = SweepBudget(max_promotions=v["max_promotions"])

    if not v["confirm_cost"]:
        estimate = estimate_sweep(v["dataset"], budget=budget)
        estimate["confirm_required"] = True
        return _run(f"estimate the cost of sweeping {v['dataset']}",
                    lambda: QueryResult([estimate], stats=None))

    def run():
        result = sweep(v["question"], dataset=v["dataset"], budget=budget,
                       start_at=v["start_at"])
        payload = result.as_dict()
        rows = [{"coverage": payload["coverage"], "progress": payload["progress"],
                 "cursor": payload["cursor"], "notes": payload["notes"]}]
        rows += [{"ranked_chunk": e} for e in payload["ranked"]]
        rows += [{"digest": d} for d in payload["digests"]]
        return QueryResult(rows, stats=result.stats)

    return _run(f"sweep {v['dataset']} for evidence about {v['question'][:60]!r}", run)


@mcp.tool(name="investigate_chunk_tool")
def investigate_chunk_tool(chunk_id: str, dataset: str = "otel_s3",
                           question: str = "") -> dict:
    """Read one named chunk's message text and digest it."""
    v = _validated(InvestigateChunk, chunk_id=chunk_id, dataset=dataset,
                   question=question)
    return _run(
        f"read message text for chunk {v['chunk_id']}",
        lambda: QueryResult(
            [investigate_chunk(v["chunk_id"], question=v["question"],
                               dataset=v["dataset"])],
            stats=None,
        ),
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
