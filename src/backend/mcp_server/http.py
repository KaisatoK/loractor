"""The HTTP deployment: the ASGI app and its unauthenticated health probe."""

from __future__ import annotations

import logging
from ipaddress import ip_address

import anyio
import fastmcp
from starlette.requests import Request
from starlette.responses import JSONResponse

from backend.mcp_server.app import mcp
from backend.mcp_server.config import MCP_HOST, MCP_PATH

logger = logging.getLogger(__name__)

HEALTH_TIMEOUT_S = 5


def _database_answers() -> None:
    from backend.db.connection import db_cursor

    with db_cursor() as cursor:
        cursor.execute("SELECT 1")


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> JSONResponse:
    """Liveness plus the log database, for a load balancer or a person with curl.

    Unauthenticated by design (FastMCP never protects custom routes) and
    therefore says nothing beyond ok / unavailable: no host, no error text.
    """
    try:
        with anyio.fail_after(HEALTH_TIMEOUT_S):
            await anyio.to_thread.run_sync(_database_answers)
    except Exception as error:  # noqa: BLE001 -- any failure means "not ready"
        logger.warning("health check failed: %s", error)
        return JSONResponse({"status": "unavailable"}, status_code=503)
    return JSONResponse({"status": "ok"})


def allowed_hosts(host: str = MCP_HOST) -> list[str] | None:
    """Host names, beyond loopback and the bound address, a request may use.

    FastMCP already accepts ``localhost``, ``127.0.0.1``, ``::1`` and the address
    the server is bound to. A wildcard bind (``0.0.0.0``) names no usable host, yet
    the pipeline's own client connects to exactly that, so it is allowed
    explicitly. Further names (a public hostname) come from FastMCP's own
    ``FASTMCP_HTTP_ALLOWED_HOSTS``; this server adds no setting of its own.
    """
    configured = list(fastmcp.settings.http_allowed_hosts or [])
    try:
        unspecified = ip_address(host).is_unspecified
    except ValueError:
        unspecified = False
    if unspecified:
        configured.append(host)
    return configured or None


def create_http_app(server=mcp, *, host: str = MCP_HOST, path: str = MCP_PATH):
    """The Streamable HTTP app, with the Host/Origin guard on.

    Without the guard a web page could reach a loopback-bound server through DNS
    rebinding; a token does not stop that on its own when none is configured.
    """
    return server.http_app(
        path=path,
        transport="streamable-http",
        host_origin_protection=True,
        allowed_hosts=allowed_hosts(host),
    )
