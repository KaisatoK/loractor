"""Counting tool.

The four original count functions collapsed into one dataset-driven builder, so
this tool now selects a breakdown dimension rather than dispatching to a
different function per column.
"""

from langchain.tools import ToolRuntime, tool

from backend.db import count_by_field
from backend.db.datasets import DEFAULT_DATASET_ID
from backend.pipeline.tools.executor import execute_database_tool
from backend.pipeline.tools.schemas import CountByCategory

_LABELS = {
    "general": "logs",
    "service": "services",
    "entity_id": "entities",
    "event_id": "event ids",
}


@tool(args_schema=CountByCategory)
def count_by_category_tool(
    runtime: ToolRuntime,
    start_time: str,
    end_time: str,
    type: str = "general",
    dataset: str = DEFAULT_DATASET_ID,
    category: str | None = None,
    limit: int = 50,
) -> dict:
    """Count log rows by severity in a time window, optionally broken down by
    service, entity, or event id.

    Cheap relative to a text search: it reads only the timestamp, severity, and
    grouping columns, so on the remote corpus it is a column-pruned aggregate
    rather than a full read. The counts cover every matching row in the scope
    (kind=aggregate), so they are measured frequencies for the stated window and
    filters; on a dataset without exact counts, ``query_stats.approximate`` says so.
    """
    operation = f"count {_LABELS.get(type, type)} on {dataset} by severity"
    if category:
        operation += f" for '{category}'"

    group_by = None if type == "general" else type
    return execute_database_tool(
        runtime=runtime,
        operation=operation,
        function=lambda: count_by_field(
            start_time, end_time, group_by, dataset, category, limit
        ),
        kind="aggregate",
        dataset=dataset,
        limit=limit,
        filters={
            "start_time": start_time,
            "end_time": end_time,
            "category": category,
            "group_by": "category" if group_by is None else f"{group_by}, category",
        },
    )
