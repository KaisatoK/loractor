import logging
from collections.abc import Callable
from typing import Any

from langchain.tools import ToolRuntime

from backend.db import *

logger = logging.getLogger(__name__)


def execute_database_tool(
    *,
    runtime: ToolRuntime,
    operation: str,
    function: Callable[[], Any],
) -> dict:
    writer = runtime.stream_writer
    writer(
        {
            "type": "tool_progress",
            "operation": operation,
            "status": "started",
            "message": f"Starting {operation}.",
        }
    )
    print("#########################################")
    print(f"Executing database tool: {operation}")
    print(f"Tool runtime: {runtime}")
    print(f"Tool function: {function}")
    print("#########################################")

    try:
        data = function()
    except Exception as e:
        logger.exception("Database tool failed: %s", operation)
        writer(
            {
                "type": "tool_progress",
                "operation": operation,
                "status": "failed",
                "message": f"{operation} failed.",
            }
        )
        raise RuntimeError(f"{operation} failed") from e

    writer(
        {
            "type": "tool_progress",
            "operation": operation,
            "status": "completed",
            "message": f"Completed {operation}.",
        }
    )
    return {"status": "success", "operation": operation, "data": data}
