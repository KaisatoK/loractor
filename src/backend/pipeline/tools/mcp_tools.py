"""LangChain tool wrappers around MCP calls.

Wrappers are deliberately local: they restore ``ToolRuntime`` progress events
and return the legacy JSON envelope consumed by result collection.
"""
from __future__ import annotations

from langchain.tools import ToolRuntime, tool

from backend.db.datasets import DEFAULT_DATASET_ID
from backend.pipeline.tools.context_tools import nearby_operation
from backend.pipeline.tools.count_tools import count_by_dataset_operation
from backend.pipeline.tools.envelope import TOOL_KINDS
from backend.pipeline.tools.executor import execute_database_tool
from backend.pipeline.tools.mcp_bridge import get_mcp_bridge
from backend.pipeline.tools.tools import search_operation
from backend.pipeline.tools.trend_tools import trend_operation
from backend.pipeline.tools.schemas import (
    NEARBY_EVENTS_DESCRIPTION,
    CountByDataset,
    ComparePeriods, CountByCategory, GetLogByLineId, GetNearbyEvents, GetTools,
    GetTrend,
    InvestigateChunk,
    SearchLogs, TopInCategoryByService,
    SweepChunks,
)


def _call(runtime: ToolRuntime, name: str, operation: str, arguments: dict) -> dict:
    """Invoke an MCP tool and keep the server's whole envelope.

    The executed query, filters, truncation flag and ``query_stats`` survive,
    and a refusal arriving over MCP is passed through as a refusal (not data),
    so the agent sees the same actionable result on both transports.
    ``execute_database_tool`` fills any field an older server omits with ``None``.
    """
    def invoke():
        return get_mcp_bridge().call_tool(name, arguments)

    return execute_database_tool(
        runtime=runtime,
        operation=operation,
        function=invoke,
        kind=TOOL_KINDS.get(name),
        dataset=arguments.get("dataset"),
        limit=arguments.get("limit"),
    )


# No args_schema: an empty one makes langchain-core drop the injected runtime
# (see catalog_tools.list_datasets_tool).
@tool(description="List every dataset's rows, time span and severity levels.")
def list_datasets_tool(runtime: ToolRuntime) -> dict:
    return _call(runtime, "list_datasets_tool",
                 "list every dataset with its rows, time span and severity levels",
                 {})


@tool(args_schema=CountByDataset,
      description="Count log entries by severity in each whole local dataset.")
def count_by_dataset_tool(runtime: ToolRuntime,
                          datasets: list[str] | None = None) -> dict:
    return _call(runtime, "count_by_dataset_tool",
                 count_by_dataset_operation(datasets), {"datasets": datasets})


@tool(args_schema=CountByCategory, description="Count log rows by severity.")
def count_by_category_tool(runtime: ToolRuntime, start_time: str, end_time: str,
                           type: str = "general", dataset: str = DEFAULT_DATASET_ID,
                           category: str | None = None, limit: int = 50) -> dict:
    return _call(runtime, "count_by_category_tool",
                 f"count {type} on {dataset} by severity",
                 {"start_time": start_time, "end_time": end_time, "type": type,
                  "dataset": dataset, "category": category, "limit": limit})


@tool(args_schema=GetTools, description="List a field's most common values.")
def get_data_tool(runtime: ToolRuntime, start_time: str, end_time: str, type: str,
                  dataset: str = DEFAULT_DATASET_ID, limit: int = 50) -> dict:
    return _call(runtime, "get_data_tool",
                 f"list the most common {type} values on {dataset}",
                 {"start_time": start_time, "end_time": end_time, "type": type,
                  "dataset": dataset, "limit": limit})


@tool(args_schema=GetLogByLineId, description="Fetch one log row by id.")
def get_log_by_line_id_tool(runtime: ToolRuntime, line_id: int,
                            dataset: str = DEFAULT_DATASET_ID) -> dict:
    return _call(runtime, "get_log_by_line_id_tool",
                 f"fetch {dataset} row with id {line_id}",
                 {"line_id": line_id, "dataset": dataset})


@tool(args_schema=GetNearbyEvents, description=NEARBY_EVENTS_DESCRIPTION)
def get_nearby_events_tool(runtime: ToolRuntime, line_id: int,
                           dataset: str = DEFAULT_DATASET_ID, before: int = 5,
                           after: int = 5, same_entity: bool = False,
                           same_request: bool = False) -> dict:
    return _call(runtime, "get_nearby_events_tool",
                 nearby_operation(line_id, dataset, before, after,
                                  same_entity, same_request),
                 {"line_id": line_id, "dataset": dataset, "before": before,
                  "after": after, "same_entity": same_entity,
                  "same_request": same_request})


@tool(args_schema=GetTrend, description="Count one field value per time bucket.")
def get_trend_tool(runtime: ToolRuntime, start_time: str, end_time: str,
                   type: tuple[str, str], dataset: str = DEFAULT_DATASET_ID,
                   bucket: str | None = None, category: str | None = None,
                   group_by: str | None = None) -> dict:
    return _call(runtime, "get_trend_tool",
                 trend_operation(type[0], type[1], dataset, category, group_by),
                 {"start_time": start_time, "end_time": end_time, "type": list(type),
                  "dataset": dataset, "bucket": bucket, "category": category,
                  "group_by": group_by})


@tool(args_schema=SearchLogs, description="Search log message content.")
def search_logs_tool(runtime: ToolRuntime, query_text: str,
                     category: str | None = None, dataset: str = DEFAULT_DATASET_ID,
                     limit: int = 10, start_time: str | None = None,
                     end_time: str | None = None) -> dict:
    return _call(runtime, "search_logs_tool",
                 search_operation(query_text, dataset, category, start_time, end_time),
                 {"query_text": query_text, "category": category,
                  "dataset": dataset, "limit": limit,
                  "start_time": start_time, "end_time": end_time})


@tool(args_schema=TopInCategoryByService,
      description="Rank a field's values by log volume.")
def top_in_category_by_service_tool(runtime: ToolRuntime, start_time: str,
                                    end_time: str, field: str = "service",
                                    dataset: str = DEFAULT_DATASET_ID,
                                    category: str | None = None,
                                    limit: int = 5) -> dict:
    return _call(runtime, "top_in_category_by_service_tool",
                 f"rank {dataset} {field} by log volume",
                 {"start_time": start_time, "end_time": end_time, "field": field,
                  "dataset": dataset, "category": category, "limit": limit})


@tool(args_schema=ComparePeriods, description="Compare two time windows.")
def compare_periods_tool(runtime: ToolRuntime, baseline_start: str, baseline_end: str,
                         recent_start: str, recent_end: str, field: str = "service",
                         dataset: str = DEFAULT_DATASET_ID, limit: int = 5) -> dict:
    return _call(runtime, "compare_periods_tool",
                 f"compare {dataset} log volume across two time windows",
                 {"baseline_start": baseline_start, "baseline_end": baseline_end,
                  "recent_start": recent_start, "recent_end": recent_end,
                  "field": field, "dataset": dataset, "limit": limit})


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


@tool(args_schema=SweepChunks,
      description="Graded pass over a whole remote corpus; ranks relevant chunks.")
def sweep_chunks_tool(runtime: ToolRuntime, question: str, dataset: str = "otel_s3",
                      max_promotions: int = 0, start_at: int = 0,
                      confirm_cost: bool = False) -> dict:
    return _call(runtime, "sweep_chunks_tool",
                 (f"estimate the cost of sweeping {dataset}" if not confirm_cost
                  else f"sweep {dataset} for evidence about {question[:60]!r}"),
                 {"question": question, "dataset": dataset,
                  "max_promotions": max_promotions, "start_at": start_at,
                  "confirm_cost": confirm_cost})


@tool(args_schema=InvestigateChunk,
      description="Read one chunk's message text and digest it.")
def investigate_chunk_tool(runtime: ToolRuntime, chunk_id: str,
                           dataset: str = "otel_s3", question: str = "") -> dict:
    return _call(runtime, "investigate_chunk_tool",
                 f"read message text for chunk {chunk_id}",
                 {"chunk_id": chunk_id, "dataset": dataset, "question": question})


MCP_DB_TOOLS = [
    list_datasets_tool, count_by_dataset_tool,
    count_by_category_tool, get_data_tool, get_log_by_line_id_tool,
    get_nearby_events_tool, get_trend_tool,
    search_logs_tool, top_in_category_by_service_tool, compare_periods_tool,
    sweep_chunks_tool, investigate_chunk_tool,
    # Planned, not registered yet (schema CountByTeam): count_by_team_tool (T3).
]
