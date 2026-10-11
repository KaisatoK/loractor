from langchain.tools import ToolRuntime, tool

from backend.db import (
    get_event_id_trend_by_category,
    get_pid_trend_by_category,
    get_service_trend_by_category,
)
from backend.db.config import TABLE_NAME
from backend.pipeline.tools.executor import execute_database_tool
from backend.pipeline.tools.schemas import GetTrend


@tool(args_schema=GetTrend)
def get_trend_tool(
    runtime: ToolRuntime,
    start_time: str,
    end_time: str,
    type: tuple[str, str],
    table_name: str = TABLE_NAME,
    category: str | None = None,
) -> dict:
    """Count one known PID by minute; optionally restrict results to one severity."""
    kind = {"pid": "pid", "service": "service", "event_id": "event id"}.get(type[0])
    operation = f"get the {kind} trend for '{type[1]}'"
    if category:
        operation += f" by '{category}' severity"

    match type[0]:
        case "pid":
            return execute_database_tool(
                runtime=runtime,
                operation=operation,
                function=lambda: get_pid_trend_by_category(
                    start_time, end_time, int(type[1]), table_name, category
                ),
            )
        case "service":
            return execute_database_tool(
                runtime=runtime,
                operation=operation,
                function=lambda: get_service_trend_by_category(
                    start_time, end_time, type[1], table_name, category
                ),
            )
        case "event_id":
            return execute_database_tool(
                runtime=runtime,
                operation=operation,
                function=lambda: get_event_id_trend_by_category(
                    start_time, end_time, type[1], table_name, category
                ),
            )
        case _:
            raise ValueError("Invalid type provided")
