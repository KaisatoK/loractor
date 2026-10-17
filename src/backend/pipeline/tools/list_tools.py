"""Value-discovery tool.

Replaces three unbounded ``SELECT DISTINCT`` helpers with one bounded,
volume-ranked lookup. The agent uses this to find a real filter value before
asking for a trend, so ranking by row count is what makes the answer useful.
"""

from langchain.tools import ToolRuntime, tool

from backend.db import get_field_values
from backend.db.datasets import DEFAULT_DATASET_ID
from backend.pipeline.tools.executor import execute_database_tool
from backend.pipeline.tools.schemas import GetTools


@tool(args_schema=GetTools)
def get_data_tool(
    runtime: ToolRuntime,
    start_time: str,
    end_time: str,
    type: str,
    dataset: str = DEFAULT_DATASET_ID,
    limit: int = 50,
) -> dict:
    """List the most common values of a field in a time window, with counts.

    Use it to discover valid filter values before requesting a trend. Fields
    whose distinct set is effectively unbounded (trace ids on the OTel corpus)
    are refused with an alternative rather than attempted. Returns kind=catalog
    rows (value, count); they are for discovery, not a frequency analysis.
    """
    return execute_database_tool(
        runtime=runtime,
        operation=f"list the most common {type} values on {dataset}",
        function=lambda: get_field_values(
            start_time, end_time, type, dataset, limit
        ),
        kind="catalog",
        dataset=dataset,
        limit=limit,
        filters={"start_time": start_time, "end_time": end_time, "field": type},
    )
