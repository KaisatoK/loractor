"""Search, fetch, ranking, and period-comparison tools.

Tool names are unchanged from before the registry, because ``TOOL_CATALOG`` in
the prompt layer is asserted to match ``tools.__all__`` and the live route cases
are coupled to that surface. What changed is the arguments: a ``dataset`` id
from a closed vocabulary instead of a table name, and a mandatory time window
wherever rows are scanned.
"""

from langchain.tools import ToolRuntime, tool

from backend.db import (
    compare_periods,
    get_log_by_record_id,
    search_logs,
    top_by_field,
)
from backend.db.datasets import DEFAULT_DATASET_ID
from backend.pipeline.tools.executor import execute_database_tool
from backend.pipeline.tools.schemas import (
    ComparePeriods,
    GetLogByLineId,
    SearchLogs,
    TopInCategoryByService,
)


@tool(args_schema=SearchLogs)
def search_logs_tool(
    query_text: str,
    runtime: ToolRuntime,
    category: str | None = None,
    dataset: str = DEFAULT_DATASET_ID,
    limit: int = 10,
) -> dict:
    """Search message text on an indexed corpus; put severity in category.

    Reads the inverted index, so it never scans the remote files. On the OTel
    corpus use the indexed slice -- the raw remote tier has no index.
    """
    operation = f"search {dataset} messages for '{query_text}'"
    if category:
        operation += f" with severity '{category}'"

    return execute_database_tool(
        runtime=runtime,
        operation=operation,
        function=lambda: search_logs(query_text, dataset, category, limit),
    )


@tool(args_schema=GetLogByLineId)
def get_log_by_line_id_tool(
    line_id: int,
    runtime: ToolRuntime,
    dataset: str = DEFAULT_DATASET_ID,
) -> dict:
    """Fetch one complete log row by its primary-key id.

    Use it to resolve a "Line ID: n" reference into the whole record. Only
    corpora that have a primary key support this; the raw OTel corpus does not.
    """
    return execute_database_tool(
        runtime=runtime,
        operation=f"fetch {dataset} row with id {line_id}",
        function=lambda: get_log_by_record_id(line_id, dataset),
    )


@tool(args_schema=TopInCategoryByService)
def top_in_category_by_service_tool(
    start_time: str,
    end_time: str,
    runtime: ToolRuntime,
    field: str = "service",
    dataset: str = DEFAULT_DATASET_ID,
    category: str | None = None,
    limit: int = 5,
) -> dict:
    """Rank services (or another field) by log volume in a time window."""
    operation = f"rank {dataset} {field} by log volume"
    if category:
        operation += f" for '{category}' severity"
    return execute_database_tool(
        runtime=runtime,
        operation=operation,
        function=lambda: top_by_field(
            start_time, end_time, field, dataset, category, limit
        ),
    )


@tool(args_schema=ComparePeriods)
def compare_periods_tool(
    baseline_start: str,
    baseline_end: str,
    recent_start: str,
    recent_end: str,
    runtime: ToolRuntime,
    field: str = "service",
    dataset: str = DEFAULT_DATASET_ID,
    limit: int = 5,
) -> dict:
    """Compare counts between a baseline and a recent window."""
    return execute_database_tool(
        runtime=runtime,
        operation=f"compare {dataset} log volume across two time windows",
        function=lambda: compare_periods(
            baseline_start,
            baseline_end,
            recent_start,
            recent_end,
            dataset,
            field,
            limit,
        ),
    )
