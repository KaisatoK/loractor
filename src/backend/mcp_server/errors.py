"""Turn a failure inside a tool into a message an agent can act on.

A refusal (``QueryRefused``) never gets here: ``run_enveloped`` already returns it
as a result. What reaches this module is a real failure. The server runs with
``mask_error_details=True``, so only a ``ToolError`` reaches the client with its
text; everything else would be reported as a generic failure. Each failure the
query layer can plausibly raise therefore becomes a ``ToolError`` that says what
happened and what to try, while the traceback stays in the server log.
"""

from __future__ import annotations

import logging

from fastmcp.exceptions import ToolError
from psycopg2 import InterfaceError, OperationalError, errors
from psycopg2.pool import PoolError

from backend.db.config import SERENEDB_STATEMENT_TIMEOUT_MS

logger = logging.getLogger(__name__)


def to_tool_error(tool_name: str, error: Exception) -> ToolError:
    """The ``ToolError`` for an exception raised while running ``tool_name``."""
    if isinstance(error, ToolError):
        return error
    if isinstance(error, errors.QueryCanceled):
        seconds = SERENEDB_STATEMENT_TIMEOUT_MS / 1000
        logger.warning("%s timed out: %s", tool_name, error)
        return ToolError(
            f"{tool_name} was cancelled: the query ran longer than {seconds:g} s. "
            "Narrow the time window, add a severity or service filter, or lower "
            "the limit, then retry."
        )
    if isinstance(error, PoolError):
        logger.warning("%s found the connection pool exhausted: %s", tool_name, error)
        return ToolError(
            f"{tool_name} could not get a database connection because the server "
            "is busy. Wait a moment and retry."
        )
    if isinstance(error, (OperationalError, InterfaceError)):
        logger.error("%s could not reach the database: %s", tool_name, error)
        return ToolError(
            f"{tool_name} could not reach the database. It may be down or "
            "restarting; retry later, and do not infer a result."
        )
    logger.exception("%s failed unexpectedly", tool_name, exc_info=error)
    return ToolError(
        f"{tool_name} failed unexpectedly (details are in the server log). "
        "Do not infer a result; retry with a narrower request."
    )
