"""Trend tool.

Buckets by ``date_trunc`` on the canonical timestamp with an explicit width,
replacing the original minute-of-hour extraction that was only meaningful
inside a single hour.
"""

from langchain.tools import ToolRuntime, tool

from backend.db import get_trend
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


@tool(args_schema=GetTrend)
def get_trend_tool(
    runtime: ToolRuntime,
    start_time: str,
    end_time: str,
    type: tuple[str, str],
    dataset: str = DEFAULT_DATASET_ID,
    bucket: str = "minute",
    category: str | None = None,
) -> dict:
    """Count one known field value per time bucket, to see how it moves.

    Pick the bucket to suit the window: 'second' for a short incident, 'hour' or
    'day' for a long one. A window that would produce too many points is refused
    with a coarser bucket suggested. Returns kind=trend rows (bucket, count):
    measured counts for every bucket in the window.
    """
    field, value = type[0], type[1]
    operation = f"get the {_LABELS.get(field, field)} trend for '{value}' on {dataset}"
    if category:
        operation += f" by '{category}' severity"

    return execute_database_tool(
        runtime=runtime,
        operation=operation,
        function=lambda: get_trend(
            start_time, end_time, field, value, dataset, bucket, category
        ),
        kind="trend",
        dataset=dataset,
        complete=True,
        filters={
            "start_time": start_time,
            "end_time": end_time,
            field: value,
            "category": category,
            "bucket": bucket,
        },
    )
