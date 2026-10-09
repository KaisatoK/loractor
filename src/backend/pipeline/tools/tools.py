from langchain.tools import ToolRuntime, tool

from backend.db import *
from backend.pipeline.tools.schemas import ComparePeriods, SearchLogs, TopInCategoryByService
from backend.pipeline.tools.utils import TABLE_NAME, execute_database_tool


@tool("search_logs", args_schema=SearchLogs)
def search_logs_tool(
    query_text: str,
    runtime: ToolRuntime,
    category: str | None = None,
    table_name: str = TABLE_NAME,
    limit: int = 10,
) -> dict:
    """Search message content; put severity in category, not query_text."""
    print("#########################################")
    print("Executing search_logs tool")
    print(f"table_name: {table_name}")
    print(f"query_text: {query_text}")
    print(f"category: {category}")
    print(f"runtime: {runtime}")
    print("#########################################")

    return execute_database_tool(
        runtime=runtime,
        operation="search log messages",
        function=lambda: search_logs(query_text, table_name, category, limit),
    )


@tool(
    "top_in_category_by_service",
    args_schema=TopInCategoryByService,
)
def top_in_category_by_service_tool(
    start_time: str,
    end_time: str,
    runtime: ToolRuntime,
    table_name: str = TABLE_NAME,
    category: str | None = None,
    limit: int = 5,
) -> dict:
    """Rank services by log volume, optionally restricted to one severity."""
    return execute_database_tool(
        runtime=runtime,
        operation="rank services by log volume",
        function=lambda: top_in_category_by_service(
            start_time, end_time, table_name, category, limit
        ),
    )


@tool("compare_periods", args_schema=ComparePeriods)
def compare_periods_tool(
    baseline_start: str,
    baseline_end: str,
    recent_start: str,
    recent_end: str,
    runtime: ToolRuntime,
    table_name: str = TABLE_NAME,
    limit: int = 5,
) -> dict:
    """Compare service/severity counts between baseline and recent windows."""
    print("#########################################")
    print("Executing compare_periods tool")
    print(f"table_name: {table_name}")
    print(f"baseline_start: {baseline_start}")
    print(f"baseline_end: {baseline_end}")
    print(f"recent_start: {recent_start}")
    print(f"recent_end: {recent_end}")
    print(f"runtime: {runtime}")
    print("#########################################")
    return execute_database_tool(
        runtime=runtime,
        operation="compare log periods",
        function=lambda: compare_periods(
            baseline_start,
            baseline_end,
            recent_start,
            recent_end,
            table_name,
            limit,
        ),
    )
