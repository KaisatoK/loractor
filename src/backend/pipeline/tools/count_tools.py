from langchain.tools import ToolRuntime, tool

from backend.db import *
from backend.pipeline.schemas import CountByCategory
from backend.pipeline.tools.utils import TABLE_NAME, execute_database_tool


@tool("count_by_category", args_schema=CountByCategory)
def count_by_category_tool(
    start_time: str,
    end_time: str,
    runtime: ToolRuntime,
    table_name: str = TABLE_NAME,
    category: str | None = None,
) -> dict:
    """Count log rows by exact severity within an inclusive time window."""
    print("#########################################")
    print("Executing count_by_category tool")
    print(f"table_name: {table_name}")
    print(f"start_time: {start_time}")
    print(f"end_time: {end_time}")
    print(f"category: {category}")
    print(f"runtime: {runtime}")
    print("#########################################")

    return execute_database_tool(
        runtime=runtime,
        operation="count logs by severity",
        function=lambda: count_by_category(start_time, end_time, table_name, category),
    )


@tool("count_pid_by_category", args_schema=CountByCategory)
def count_pid_by_category_tool(
    start_time: str,
    end_time: str,
    runtime: ToolRuntime,
    table_name: str = TABLE_NAME,
    category: str | None = None,
) -> dict:
    """Count log rows by exact severity within an inclusive time window group by PID."""
    print("#########################################")
    print("Executing count_pid_by_category tool")
    print(f"table_name: {table_name}")
    print(f"start_time: {start_time}")
    print(f"end_time: {end_time}")
    print(f"category: {category}")
    print(f"runtime: {runtime}")
    print("#########################################")

    return execute_database_tool(
        runtime=runtime,
        operation="count pid by severity",
        function=lambda: count_pid_by_category(
            start_time, end_time, table_name, category
        ),
    )


@tool(
    "count_service_by_category",
    args_schema=CountByCategory,
)
def count_service_by_category_tool(
    start_time: str,
    end_time: str,
    runtime: ToolRuntime,
    table_name: str = TABLE_NAME,
    category: str | None = None,
) -> dict:
    """Count log rows by exact severity within an inclusive time window group by service."""
    print("#########################################")
    print("Executing count_service_by_category tool")
    print(f"table_name: {table_name}")
    print(f"start_time: {start_time}")
    print(f"end_time: {end_time}")
    print(f"category: {category}")
    print(f"runtime: {runtime}")
    print("#########################################")

    return execute_database_tool(
        runtime=runtime,
        operation="count service by severity",
        function=lambda: count_service_by_category(
            start_time, end_time, table_name, category
        ),
    )


@tool(
    "count_event_id_by_category",
    args_schema=CountByCategory,
)
def count_event_id_by_category_tool(
    start_time: str,
    end_time: str,
    runtime: ToolRuntime,
    table_name: str = TABLE_NAME,
    category: str | None = None,
) -> dict:
    """Count log rows by exact severity within an inclusive time window group by event ID."""
    print("#########################################")
    print("Executing count_event_id_by_category tool")
    print(f"table_name: {table_name}")
    print(f"start_time: {start_time}")
    print(f"end_time: {end_time}")
    print(f"category: {category}")
    print(f"runtime: {runtime}")
    print("#########################################")

    return execute_database_tool(
        runtime=runtime,
        operation="count event_id by severity",
        function=lambda: count_event_id_by_category(
            start_time, end_time, table_name, category
        ),
    )
