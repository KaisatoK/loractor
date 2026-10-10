from langchain.tools import ToolRuntime, tool

from backend.db import *
from backend.pipeline.tools.schemas import GetTools
from backend.pipeline.utils import (
    TABLE_NAME,
    execute_database_tool,
)


@tool(args_schema=GetTools)
def get_data_tool(
    runtime: ToolRuntime,
    type: str,
    table_name: str = TABLE_NAME,
) -> dict:
    """Get all data of a specific type (pid or service or event_id) in the log."""
    print("#########################################")
    print("Executing get_data tool")
    print(f"table_name: {table_name}")
    print(f"type: {type}")
    print("#########################################")

    match type:
        case "pid": return execute_database_tool(
            runtime=runtime,
            operation="get pids",
            function=lambda: get_pids(table_name),
        )
        case "service": return execute_database_tool(
            runtime=runtime,
            operation="get services",
            function=lambda: get_services(table_name),
        )
        case "event_id": return execute_database_tool(
            runtime=runtime,
            operation="get event_ids",
            function=lambda: get_event_ids(table_name),
        )

# @tool("get_pids", args_schema=GetTools)
# def get_pids_tool(
#     runtime: ToolRuntime,
#     table_name: str = TABLE_NAME,
# ) -> dict:
#     """Get all PIDs in the log."""
#     print("#########################################")
#     print("Executing get_pids tool")
#     print(f"table_name: {table_name}")
#     print("#########################################")

#     return execute_database_tool(
#         runtime=runtime,
#         operation="get pids",
#         function=lambda: get_pids(table_name),
#     )


# @tool("get_services", args_schema=GetTools)
# def get_services_tool(
#     runtime: ToolRuntime,
#     table_name: str = TABLE_NAME,
# ) -> dict:
#     """Get all services name in the log."""
#     print("#########################################")
#     print("Executing get_services tool")
#     print(f"table_name: {table_name}")
#     print("#########################################")

#     return execute_database_tool(
#         runtime=runtime,
#         operation="get services",
#         function=lambda: get_services(table_name),
#     )


# @tool("get_event_ids", args_schema=GetTools)
# def get_event_ids_tool(
#     runtime: ToolRuntime,
#     table_name: str = TABLE_NAME,
# ) -> dict:
#     """Get all event_ids in the log."""
#     print("#########################################")
#     print("Executing get_event_ids tool")
#     print(f"table_name: {table_name}")
#     print("#########################################")

#     return execute_database_tool(
#         runtime=runtime,
#         operation="get event_ids",
#         function=lambda: get_event_ids(table_name),
#     )
