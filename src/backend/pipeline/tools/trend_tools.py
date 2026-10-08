from langchain.tools import ToolRuntime, tool

from backend.db import *
from backend.pipeline.tools.utils import load_tool_schema, execute_database_tool

TABLE_NAME = "log"


@tool("get_pid_trend", args_schema=load_tool_schema("get_pid_trend"))
def get_pid_trend_tool(
    start_time: str,
    end_time: str,
    pid: int,
    runtime: ToolRuntime,
    table_name: str = TABLE_NAME,
    category: str | None = None,
) -> dict:
    """Count one known PID by minute; optionally restrict results to one severity."""
    print("#########################################")
    print("Executing get_pid_trend tool")
    print(f"table_name: {table_name}")
    print(f"start_time: {start_time}")
    print(f"end_time: {end_time}")
    print(f"pid: {pid}")
    print(f"category: {category}")
    print(f"runtime: {runtime}")
    print("#########################################")

    return execute_database_tool(
        runtime=runtime,
        operation="get PID log trend",
        function=lambda: get_pid_trend_by_category(
            start_time, end_time, pid, table_name, category
        ),
    )


@tool("get_service_trend", args_schema=load_tool_schema("get_service_trend"))
def get_service_trend_tool(
    start_time: str,
    end_time: str,
    service: str,
    runtime: ToolRuntime,
    table_name: str = TABLE_NAME,
    category: str | None = None,
) -> dict:
    """Count one known service by minute; optionally restrict results to one severity."""
    print("#########################################")
    print("Executing get_service_trend tool")
    print(f"table_name: {table_name}")
    print(f"start_time: {start_time}")
    print(f"end_time: {end_time}")
    print(f"service: {service}")
    print(f"category: {category}")
    print(f"runtime: {runtime}")
    print("#########################################")

    return execute_database_tool(
        runtime=runtime,
        operation="get service log trend",
        function=lambda: get_service_trend_by_category(
            start_time, end_time, service, table_name, category
        ),
    )


@tool("get_event_id_trend", args_schema=load_tool_schema("get_event_id_trend"))
def get_event_id_trend_tool(
    start_time: str,
    end_time: str,
    event_id: str,
    runtime: ToolRuntime,
    table_name: str = TABLE_NAME,
    category: str | None = None,
) -> dict:
    """Count one known event_id by minute; optionally restrict results to one severity."""
    print("#########################################")
    print("Executing get_event_id_trend tool")
    print(f"table_name: {table_name}")
    print(f"start_time: {start_time}")
    print(f"end_time: {end_time}")
    print(f"event_id: {event_id}")
    print(f"category: {category}")
    print(f"runtime: {runtime}")
    print("#########################################")

    return execute_database_tool(
        runtime=runtime,
        operation="get event_id log trend",
        function=lambda: get_event_id_trend_by_category(
            start_time, end_time, event_id, table_name, category
        ),
    )
