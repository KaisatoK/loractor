"""The FastMCP application object. Tools, prompts and resources register on it."""

from __future__ import annotations

from fastmcp import FastMCP

mcp = FastMCP("serenedb-logs", on_duplicate="error")
