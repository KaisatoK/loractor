from langchain.tools import ToolRuntime, tool

from backend.db import *
from backend.pipeline.tools.schemas import CountByCategory
from backend.pipeline.utils import TABLE_NAME, execute_database_tool


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
    print("#########################################")
    print("Executing count_by_category tool")
    print(f"table_name: {table_name}")
    print(f"start_time: {start_time}")
    print(f"end_time: {end_time}")
    print(f"category: {category}")
    print("#########################################")

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


# @tool("count_pid_by_category", args_schema=CountByCategory)
# def count_pid_by_category_tool(
#     start_time: str,
#     end_time: str,
#     runtime: ToolRuntime,
#     table_name: str = TABLE_NAME,
#     category: str | None = None,
# ) -> dict:
#     """Count log rows by exact severity within an inclusive time window group by PID."""
#     print("#########################################")
#     print("Executing count_pid_by_category tool")
#     print(f"table_name: {table_name}")
#     print(f"start_time: {start_time}")
#     print(f"end_time: {end_time}")
#     print(f"category: {category}")
#     print(f"runtime: {runtime}")
#     print("#########################################")

#     return execute_database_tool(
#         runtime=runtime,
#         operation="count pid by severity",
#         function=lambda: count_pid_by_category(
#             start_time, end_time, table_name, category
#         ),
#     )


# @tool(
#     "count_service_by_category",
#     args_schema=CountByCategory,
# )
# def count_service_by_category_tool(
#     start_time: str,
#     end_time: str,
#     runtime: ToolRuntime,
#     table_name: str = TABLE_NAME,
#     category: str | None = None,
# ) -> dict:
#     """Count log rows by exact severity within an inclusive time window group by service."""
#     print("#########################################")
#     print("Executing count_service_by_category tool")
#     print(f"table_name: {table_name}")
#     print(f"start_time: {start_time}")
#     print(f"end_time: {end_time}")
#     print(f"category: {category}")
#     print(f"runtime: {runtime}")
#     print("#########################################")

#     return execute_database_tool(
#         runtime=runtime,
#         operation="count service by severity",
#         function=lambda: count_service_by_category(
#             start_time, end_time, table_name, category
#         ),
#     )


# @tool(
#     "count_event_id_by_category",
#     args_schema=CountByCategory,
# )
# def count_event_id_by_category_tool(
#     start_time: str,
#     end_time: str,
#     runtime: ToolRuntime,
#     table_name: str = TABLE_NAME,
#     category: str | None = None,
# ) -> dict:
#     """Count log rows by exact severity within an inclusive time window group by event ID."""
#     print("#########################################")
#     print("Executing count_event_id_by_category tool")
#     print(f"table_name: {table_name}")
#     print(f"start_time: {start_time}")
#     print(f"end_time: {end_time}")
#     print(f"category: {category}")
#     print(f"runtime: {runtime}")
#     print("#########################################")

#     return execute_database_tool(
#         runtime=runtime,
#         operation="count event_id by severity",
#         function=lambda: count_event_id_by_category(
#             start_time, end_time, table_name, category
#         ),
#     )
