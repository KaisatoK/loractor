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
    count_by_category,
    count_event_id_by_category,
    count_pid_by_category,
    count_service_by_category,
    get_event_id_trend_by_category,
    get_event_ids,
    get_log_by_line_id,
    get_pid_trend_by_category,
    get_pids,
    get_service_trend_by_category,
    get_services,
    get_log_by_line_id as fetch_log,
    search_logs,
    top_in_category_by_service,
)
from backend.db.config import TABLE_NAME
from backend.pipeline.tools.schemas import (
    ComparePeriods, CountByCategory, GetLogByLineId, GetTools, GetTrend,
    SearchLogs, TopInCategoryByService,
)
from backend.mcp_server.config import MCP_SKILLS_ROOT

mcp = FastMCP("serenedb-logs")


def _validated(model, **values: Any) -> dict[str, Any]:
    return model.model_validate(values).model_dump()


def _result(operation: str, data: Any) -> dict[str, Any]:
    return {"status": "success", "operation": operation, "data": data}


@mcp.tool(name="search_logs_tool")
def search_logs_tool(query_text: str, category: str | None = None,
                     table_name: str = TABLE_NAME, limit: int = 10) -> dict:
    """Search log message content, optionally restricted to a severity."""
    values = _validated(SearchLogs, query_text=query_text, category=category,
                        table_name=table_name, limit=limit)
    query_text, category, table_name, limit = (values[k] for k in ("query_text", "category", "table_name", "limit"))
    operation = f"search log messages for '{query_text}'"
    if category:
        operation += f" with severity '{category}'"
    return _result(operation, search_logs(query_text, table_name, category, limit))


@mcp.tool(name="get_log_by_line_id_tool")
def get_log_by_line_id_tool(line_id: int, table_name: str = TABLE_NAME) -> dict:
    """Fetch one complete log row by its line_id."""
    values = _validated(GetLogByLineId, line_id=line_id, table_name=table_name)
    line_id, table_name = values["line_id"], values["table_name"]
    return _result(f"fetch the log row with line_id {line_id}", fetch_log(line_id, table_name))


@mcp.tool(name="top_in_category_by_service_tool")
def top_in_category_by_service_tool(start_time: str, end_time: str,
                                    table_name: str = TABLE_NAME,
                                    category: str | None = None, limit: int = 5) -> dict:
    values = _validated(TopInCategoryByService, start_time=start_time, end_time=end_time,
                        table_name=table_name, category=category, limit=limit)
    start_time, end_time, table_name = values["start_time"], values["end_time"], values["table_name"]
    category, limit = values["category"], values["limit"]
    return _result("rank services by log volume", top_in_category_by_service(
        start_time, end_time, table_name, category, limit))


@mcp.tool(name="compare_periods_tool")
def compare_periods_tool(baseline_start: str, baseline_end: str,
                         recent_start: str, recent_end: str,
                         table_name: str = TABLE_NAME, limit: int = 5) -> dict:
    values = _validated(ComparePeriods, baseline_start=baseline_start, baseline_end=baseline_end,
                        recent_start=recent_start, recent_end=recent_end,
                        table_name=table_name, limit=limit)
    baseline_start, baseline_end = values["baseline_start"], values["baseline_end"]
    recent_start, recent_end = values["recent_start"], values["recent_end"]
    table_name, limit = values["table_name"], values["limit"]
    return _result("compare log volume across two time windows", compare_periods(
        baseline_start, baseline_end, recent_start, recent_end, table_name, limit))


def _count_operation(kind: str, category: str | None) -> str:
    return f"count {kind} for '{category}' severity" if category else f"count {kind} by severity"


@mcp.tool(name="count_by_category_tool")
def count_by_category_tool(start_time: str, end_time: str, type: str,
                           table_name: str = TABLE_NAME,
                           category: str | None = None) -> dict:
    values = _validated(CountByCategory, start_time=start_time, end_time=end_time,
                        type=type, table_name=table_name, category=category)
    start_time, end_time, type = values["start_time"], values["end_time"], values["type"]
    table_name, category = values["table_name"], values["category"]
    functions = {"pid": count_pid_by_category, "service": count_service_by_category,
                 "event_id": count_event_id_by_category, "general": count_by_category}
    if type not in functions:
        raise ValueError("type must be pid, service, event_id, or general")
    return _result(_count_operation(type, category), functions[type](
        start_time, end_time, table_name, category))


@mcp.tool(name="get_data_tool")
def get_data_tool(type: str, table_name: str = TABLE_NAME) -> dict:
    """Get all known pids, services, or event ids."""
    values = _validated(GetTools, type=type, table_name=table_name)
    type, table_name = values["type"], values["table_name"]
    functions = {"pid": get_pids, "service": get_services, "event_id": get_event_ids}
    if type not in functions:
        raise ValueError("type must be pid, service, or event_id")
    return _result(f"list the known {type}s", functions[type](table_name))


@mcp.tool(name="get_trend_tool")
def get_trend_tool(start_time: str, end_time: str, type: tuple[str, str],
                  table_name: str = TABLE_NAME, category: str | None = None) -> dict:
    """Count a known pid, service, or event id by minute."""
    values = _validated(GetTrend, start_time=start_time, end_time=end_time, type=type,
                        table_name=table_name, category=category)
    start_time, end_time, type = values["start_time"], values["end_time"], values["type"]
    table_name, category = values["table_name"], values["category"]
    functions = {"pid": (get_pid_trend_by_category, lambda value: int(value)),
                 "service": (get_service_trend_by_category, lambda value: value),
                 "event_id": (get_event_id_trend_by_category, lambda value: value)}
    fn, convert = functions[type[0]]
    return _result(f"get the {type[0]} trend for '{type[1]}'", fn(
        start_time, end_time, convert(type[1]), table_name, category))


@mcp.tool(name="auto_complete")
def auto_complete_tool(query: str, table_name: str = TABLE_NAME) -> dict:
    """Suggest indexed log values for a selector-prefixed query."""
    return _result(f"auto-complete '{query}'", auto_complete(query, table_name))


_SKILLS_ROOT = Path(MCP_SKILLS_ROOT) if MCP_SKILLS_ROOT else Path(__file__).resolve().parents[3] / "skills"


def _skill_files() -> dict[str, Path]:
    if not _SKILLS_ROOT.is_dir():
        raise RuntimeError(f"Skills directory not found: {_SKILLS_ROOT}")
    return {p.parent.name: p for p in _SKILLS_ROOT.glob("*/SKILL.md") if p.is_file()}


def _skill_body(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    return re.sub(r"^---\s*\n.*?\n---\s*\n", "", text, count=1, flags=re.DOTALL)


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
