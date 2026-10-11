from langchain.tools import ToolRuntime, tool

from backend.db import (
    compare_periods,
    search_logs,
    top_in_category_by_service,
)
from backend.db.config import TABLE_NAME
from backend.pipeline.tools.executor import execute_database_tool
from backend.pipeline.tools.schemas import (
    ComparePeriods,
    SearchLogs,
    TopInCategoryByService,
)


@tool(args_schema=SearchLogs)
def search_logs_tool(
    query_text: str,
    runtime: ToolRuntime,
    category: str | None = None,
    table_name: str = TABLE_NAME,
    limit: int = 10,
) -> dict:
    """Search message content; put severity in category, not query_text."""
    operation = f"search log messages for '{query_text}'"
    if category:
        operation += f" with severity '{category}'"

    return execute_database_tool(
        runtime=runtime,
        operation=operation,
        function=lambda: search_logs(query_text, table_name, category, limit),
    )


@tool(
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
    operation = "rank services by log volume"
    if category:
        operation += f" for '{category}' severity"
    return execute_database_tool(
        runtime=runtime,
        operation=operation,
        function=lambda: top_in_category_by_service(
            start_time, end_time, table_name, category, limit
        ),
    )


@tool(args_schema=ComparePeriods)
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
    return execute_database_tool(
        runtime=runtime,
        operation="compare log volume across two time windows",
        function=lambda: compare_periods(
            baseline_start,
            baseline_end,
            recent_start,
            recent_end,
            table_name,
            limit,
        ),
    )
