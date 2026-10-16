#!/bin/bash

if command -v uv &> /dev/null; then
    uv run python -m backend.mcp_server
else
    python -m backend.mcp_server
fi
