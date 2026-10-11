from langchain.tools import ToolRuntime, tool

from backend.db import (
    count_by_category,
    count_event_id_by_category,
    count_pid_by_category,
    count_service_by_category,
)
from backend.db.config import TABLE_NAME
from backend.pipeline.tools.executor import execute_database_tool
from backend.pipeline.tools.schemas import CountByCategory


@tool(args_schema=CountByCategory)
def count_by_category_tool(
    runtime: ToolRuntime,
    start_time: str,
    end_time: str,
    type: str,
    table_name: str = TABLE_NAME,
    category: str | None = None,
) -> dict:
    """Count log rows by exact severity within an inclusive time window group by pid, service, event ID or in general."""
    sev = f"by '{category}' severity" if category else None

    match type:
        case "pid":
            return execute_database_tool(
                runtime=runtime,
                operation=f"count pids {sev}" if sev else "count pids by severity",
                function=lambda: count_pid_by_category(
                    start_time, end_time, table_name, category
                ),
            )
        case "service":
            return execute_database_tool(
                runtime=runtime,
                operation=(
                    f"count services {sev}" if sev else "count services by severity"
                ),
                function=lambda: count_service_by_category(
                    start_time, end_time, table_name, category
                ),
            )
        case "event_id":
            return execute_database_tool(
                runtime=runtime,
                operation=(
                    f"count event ids {sev}" if sev else "count event ids by severity"
                ),
                function=lambda: count_event_id_by_category(
                    start_time, end_time, table_name, category
                ),
            )
        case "general":
            return execute_database_tool(
                runtime=runtime,
                operation=f"count log {sev}" if sev else "count logs by severity",
                function=lambda: count_by_category(
                    start_time, end_time, table_name, category
                ),
            )
