"""Execution wrapper for database-backed agent tools."""

import logging
from collections.abc import Callable
from typing import Any

from langchain.tools import ToolRuntime

logger = logging.getLogger(__name__)


def execute_database_tool(
    *,
    runtime: ToolRuntime,
    operation: str,
    function: Callable[[], Any],
) -> dict:
    """Run a database function and emit tool-progress events around it.

    Progress events are written to the graph's custom stream so the UI can show
    per-operation status. Failures are re-raised as a generic ``RuntimeError``;
    ``handle_tool_errors`` turns that into a ToolMessage for the model.
    """
    writer = runtime.stream_writer
    writer(
        {
            "type": "tool_progress",
            "operation": operation,
            "status": "started",
            "message": f"Starting {operation}.",
        }
    )
    logger.info("Executing database tool: %s", operation)

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
