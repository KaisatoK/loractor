"""The FastMCP application object. Tools, prompts and resources register on it."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from fastmcp import FastMCP

from backend.mcp_server.auth import build_auth
from backend.mcp_server.instructions import build_instructions
from backend.mcp_server.operations import middleware, server_lifespan

try:
    SERVER_VERSION = version("ai-chatbot-test")
except PackageNotFoundError:  # running from a checkout that was never installed
    SERVER_VERSION = "0"

WEBSITE_URL = "https://github.com/KaisatoK/ai-chatbot-test"

mcp = FastMCP(
    "serenedb-logs",
    instructions=build_instructions(),
    # Bearer token over HTTP when MCP_AUTH_TOKEN is set; ignored over stdio.
    auth=build_auth(),
    version=SERVER_VERSION,
    # Shown by clients that display server info. The repository is private until
    # it is made public; the link works once it is.
    website_url=WEBSITE_URL,
    # A duplicate registration is a bug (two tools with one name), not something
    # to warn about and carry on.
    on_duplicate="error",
    # Only a ToolError's text reaches a client; see mcp_server/errors.py.
    mask_error_details=True,
    # The database pool opens and closes with the server.
    lifespan=server_lifespan,
    middleware=middleware(),
)
