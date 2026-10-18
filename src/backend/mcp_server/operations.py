"""Operational behaviour of the server: lifespan, logging, timing, limits, caching.

Nothing here changes what a tool returns. It decides how the server behaves under
use: the database pool lives exactly as long as the server, every request is
logged and timed without its payload (tool arguments can name log text), a client
cannot hammer the shared pool, and the one expensive, slowly-changing answer is
reused for a few minutes.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import Middleware, MiddlewareContext
from fastmcp.server.middleware.caching import ResponseCachingMiddleware
from fastmcp.server.middleware.logging import StructuredLoggingMiddleware
from fastmcp.server.middleware.rate_limiting import RateLimitingMiddleware
from fastmcp.server.middleware.timing import TimingMiddleware

from backend.mcp_server import config

logger = logging.getLogger(__name__)

# The only tool whose answer is cached: whole-dataset severity counts change only
# when a corpus is reloaded. ``list_datasets_tool`` documents "measured on each
# call" and stays uncached.
CACHED_TOOLS = ["count_by_dataset_tool"]


@asynccontextmanager
async def server_lifespan(_server: Any) -> AsyncIterator[dict]:
    """Open the SereneDB pool with the server and close it when the server stops.

    A database that is down at start-up is not fatal: ``/health`` reports 503 and
    the pool is built on the first query that finds it back.
    """
    from backend.db import connection

    try:
        await asyncio.to_thread(connection.warm_pool)
    except Exception:  # noqa: BLE001 - the server must still start
        logger.warning("SereneDB pool could not be opened at start-up", exc_info=True)
    try:
        yield {}
    finally:
        await asyncio.to_thread(connection.close_pool)


def client_id(_context: MiddlewareContext) -> str:
    """Who a request counts against: a digest of the bearer token, else one shared id.

    The token itself is never stored or logged. Without auth (loopback or stdio)
    there is one client.
    """
    token = get_access_token()
    raw = getattr(token, "token", None) if token is not None else None
    if not raw:
        return "anonymous"
    return "token:" + hashlib.sha256(raw.encode()).hexdigest()[:12]


def middleware() -> list[Middleware]:
    """The server's middleware, outermost first."""
    stack: list[Middleware] = [
        # No payloads: tool arguments may carry log text or identifiers.
        StructuredLoggingMiddleware(
            logger=logging.getLogger("backend.mcp_server.requests"),
            include_payloads=False,
        ),
        TimingMiddleware(logger=logging.getLogger("backend.mcp_server.timing")),
    ]
    if config.MCP_RATE_LIMIT_RPS > 0:
        stack.append(
            RateLimitingMiddleware(
                max_requests_per_second=config.MCP_RATE_LIMIT_RPS,
                burst_capacity=max(1, int(config.MCP_RATE_LIMIT_RPS * 2)),
                get_client_id=client_id,
            )
        )
    if config.MCP_CACHE_TTL_S > 0:
        off = {"enabled": False}
        stack.append(
            ResponseCachingMiddleware(
                list_tools_settings=off,
                list_resources_settings=off,
                list_prompts_settings=off,
                read_resource_settings=off,
                get_prompt_settings=off,
                call_tool_settings={
                    "ttl": config.MCP_CACHE_TTL_S,
                    "included_tools": CACHED_TOOLS,
                },
            )
        )
    return stack
