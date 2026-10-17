"""The FastMCP application object. Tools, prompts and resources register on it."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from fastmcp import FastMCP

from backend.mcp_server.auth import build_auth
from backend.mcp_server.instructions import build_instructions

try:
    SERVER_VERSION = version("ai-chatbot-test")
except PackageNotFoundError:  # running from a checkout that was never installed
    SERVER_VERSION = "0"

mcp = FastMCP(
    "serenedb-logs",
    instructions=build_instructions(),
    # Bearer token over HTTP when MCP_AUTH_TOKEN is set; ignored over stdio.
    auth=build_auth(),
    version=SERVER_VERSION,
    # A duplicate registration is a bug (two tools with one name), not something
    # to warn about and carry on.
    on_duplicate="error",
    # Only a ToolError's text reaches a client; see mcp_server/errors.py.
    mask_error_details=True,
)
