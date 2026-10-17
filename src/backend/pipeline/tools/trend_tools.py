"""Trend tool.

Buckets by ``date_trunc`` on the canonical timestamp with an explicit width,
replacing the original minute-of-hour extraction that was only meaningful
inside a single hour. Every bucket in the window is returned, empty ones as 0.
"""

from langchain.tools import ToolRuntime, tool

from backend.db import add_burst_summary, get_trend
from backend.db.datasets import DEFAULT_DATASET_ID
from backend.pipeline.tools.executor import execute_database_tool
from backend.pipeline.tools.schemas import GetTrend

_LABELS = {
    "service": "service",
    "entity_id": "entity",
    "event_id": "event id",
    "template": "template",
    "severity": "severity",
}


def trend_operation(field: str, value: str, dataset: str,
                    category: str | None, group_by: str | None) -> str:
    operation = f"get the {_LABELS.get(field, field)} trend for '{value}' on {dataset}"
    if category:
        operation += f" by '{category}' severity"
    if group_by:
        operation += f" per {group_by}"
    return operation


def trend_filters(start_time: str, end_time: str, field: str, value: str,
                  category: str | None, bucket: str | None,
                  group_by: str | None) -> dict:
    return {
        "start_time": start_time,
        "end_time": end_time,
        field: value,
        "category": category,
        # 'auto' when the window chose it; query_stats.notes names the bucket.
        "bucket": bucket or "auto",
        "group_by": group_by,
    }


@tool(args_schema=GetTrend)
def get_trend_tool(
    runtime: ToolRuntime,
    start_time: str,
    end_time: str,
    type: tuple[str, str],
    dataset: str = DEFAULT_DATASET_ID,
    bucket: str | None = None,
    category: str | None = None,
    group_by: str | None = None,
) -> dict:
    """Count one known field value per time bucket, to see how it moves.

    Omit bucket to let the window pick one (about 100 points at most), or set
    it: 'second' for a short incident, 'hour', 'day', 'week' or 'month' for a
    long one. A window needing too many points is refused with a coarser bucket
    suggested. group_by='severity' gives one series per severity level present.
    Returns kind=trend rows (bucket, [severity,] count, prev_count, trend):
    measured counts for EVERY bucket in the window, 0 where nothing was logged.
    query_stats.notes also summarizes consecutive non-zero bucket runs and their
    measured totals.
    """
    field, value = type[0], type[1]
    return execute_database_tool(
        runtime=runtime,
        operation=trend_operation(field, value, dataset, category, group_by),
        function=lambda: add_burst_summary(
            get_trend(
                start_time, end_time, field, value, dataset, bucket, category, group_by
            ),
            group_by=group_by,
        ),
        kind="trend",
        dataset=dataset,
        complete=True,
        filters=trend_filters(
            start_time, end_time, field, value, category, bucket, group_by
        ),
    )
