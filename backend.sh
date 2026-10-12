#!/bin/bash

# Backend port: BACKEND_PORT from the environment or .env, default 8000.
PORT="${BACKEND_PORT:-}"
if [ -z "$PORT" ] && [ -f .env ]; then
    PORT="$(grep -E '^[[:space:]]*BACKEND_PORT[[:space:]]*=' .env | tail -n1 | cut -d= -f2- | xargs)"
fi
PORT="${PORT:-8000}"

if command -v uv &> /dev/null; then
    uv run python -m backend.appdb.manage abc 12345678
    uv run uvicorn backend.api:app --port "$PORT" --reload
else
    python -m backend.appdb.manage abc 12345678
    uvicorn backend.api:app --port "$PORT" --reload
fi
