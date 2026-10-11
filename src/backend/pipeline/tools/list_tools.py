from langchain.tools import ToolRuntime, tool

from backend.db import get_event_ids, get_pids, get_services
from backend.db.config import TABLE_NAME
from backend.pipeline.tools.executor import execute_database_tool
from backend.pipeline.tools.schemas import GetTools


@tool(args_schema=GetTools)
def get_data_tool(
    runtime: ToolRuntime,
    type: str,
    table_name: str = TABLE_NAME,
) -> dict:
    """Get all data of a specific type (pid or service or event_id) in the log."""
    match type:
        case "pid":
            return execute_database_tool(
                runtime=runtime,
                operation="list the known pids",
                function=lambda: get_pids(table_name),
            )
        case "service":
            return execute_database_tool(
                runtime=runtime,
                operation="list the known services",
                function=lambda: get_services(table_name),
            )
        case "event_id":
            return execute_database_tool(
                runtime=runtime,
                operation="list the known event ids",
                function=lambda: get_event_ids(table_name),
            )
