#!/bin/bash

if command -v uv &> /dev/null; then
    uv run python -m backend.mcp_server
else
    echo "uv is required to run the MCP server: https://docs.astral.sh/uv/" >&2
    exit 1
fi
