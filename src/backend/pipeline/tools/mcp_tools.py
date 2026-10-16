"""LangChain tool wrappers around MCP calls.

Wrappers are deliberately local: they restore ``ToolRuntime`` progress events
and return the legacy JSON envelope consumed by result collection.
"""
from __future__ import annotations

from langchain.tools import ToolRuntime, tool

from backend.db.config import TABLE_NAME
from backend.pipeline.tools.executor import execute_database_tool
from backend.pipeline.tools.mcp_bridge import get_mcp_bridge
from backend.pipeline.tools.schemas import (
    ComparePeriods, CountByCategory, GetLogByLineId, GetTools, GetTrend,
    SearchLogs, TopInCategoryByService,
)


def _call(runtime: ToolRuntime, name: str, operation: str, arguments: dict) -> dict:
    def invoke():
        payload = get_mcp_bridge().call_tool(name, arguments)
        return payload.get("data", payload) if isinstance(payload, dict) else payload

    return execute_database_tool(
        runtime=runtime, operation=operation, function=invoke
    )


@tool(args_schema=CountByCategory, description="Count log rows by category.")
def count_by_category_tool(runtime: ToolRuntime, start_time: str, end_time: str,
                          type: str, table_name: str = TABLE_NAME,
                          category: str | None = None) -> dict:
    return _call(runtime, "count_by_category_tool", f"count {type} by severity",
                 {"start_time": start_time, "end_time": end_time, "type": type,
                  "table_name": table_name, "category": category})


@tool(args_schema=GetTools, description="List known log identifiers.")
def get_data_tool(runtime: ToolRuntime, type: str,
                  table_name: str = TABLE_NAME) -> dict:
    return _call(runtime, "get_data_tool", f"list the known {type}s",
                 {"type": type, "table_name": table_name})


@tool(args_schema=GetLogByLineId, description="Fetch a complete log row by line id.")
def get_log_by_line_id_tool(runtime: ToolRuntime, line_id: int,
                           table_name: str = TABLE_NAME) -> dict:
    return _call(runtime, "get_log_by_line_id_tool", f"fetch the log row with line_id {line_id}",
                 {"line_id": line_id, "table_name": table_name})


@tool(args_schema=GetTrend, description="Get a log trend.")
def get_trend_tool(runtime: ToolRuntime, start_time: str, end_time: str,
                   type: tuple[str, str], table_name: str = TABLE_NAME,
                   category: str | None = None) -> dict:
    return _call(runtime, "get_trend_tool", f"get the {type[0]} trend for '{type[1]}'",
                 {"start_time": start_time, "end_time": end_time, "type": type,
                  "table_name": table_name, "category": category})


@tool(args_schema=SearchLogs, description="Search log messages.")
def search_logs_tool(runtime: ToolRuntime, query_text: str,
                     category: str | None = None, table_name: str = TABLE_NAME,
                     limit: int = 10) -> dict:
    return _call(runtime, "search_logs_tool", f"search log messages for '{query_text}'",
                 {"query_text": query_text, "category": category,
                  "table_name": table_name, "limit": limit})


@tool(args_schema=TopInCategoryByService, description="Rank services by log volume.")
def top_in_category_by_service_tool(runtime: ToolRuntime, start_time: str, end_time: str,
                                    table_name: str = TABLE_NAME,
                                    category: str | None = None, limit: int = 5) -> dict:
    return _call(runtime, "top_in_category_by_service_tool", "rank services by log volume",
                 {"start_time": start_time, "end_time": end_time, "table_name": table_name,
                  "category": category, "limit": limit})


@tool(args_schema=ComparePeriods, description="Compare two log periods.")
def compare_periods_tool(runtime: ToolRuntime, baseline_start: str, baseline_end: str,
                         recent_start: str, recent_end: str,
                         table_name: str = TABLE_NAME, limit: int = 5) -> dict:
    return _call(runtime, "compare_periods_tool", "compare log volume across two time windows",
                 {"baseline_start": baseline_start, "baseline_end": baseline_end,
                  "recent_start": recent_start, "recent_end": recent_end,
                  "table_name": table_name, "limit": limit})


@tool
def read_skill(name: str) -> str:
    """Read an investigation skill exposed by the MCP server."""
    result = get_mcp_bridge().call_tool("read_skill", {"name": name})
    return result.get("data", result) if isinstance(result, dict) else result


@tool
def list_skills() -> str:
    """List skills exposed by the MCP server."""
    result = get_mcp_bridge().call_tool("list_skills", {})
    return result.get("data", result) if isinstance(result, dict) else result


MCP_DB_TOOLS = [
    count_by_category_tool, get_data_tool, get_log_by_line_id_tool, get_trend_tool,
    search_logs_tool, top_in_category_by_service_tool, compare_periods_tool,
]
