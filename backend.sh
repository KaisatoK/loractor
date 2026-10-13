#!/bin/bash

# Backend port: BACKEND_PORT from the environment or .env, default 8000.
PORT="${BACKEND_PORT:-}"
if [ -z "$PORT" ] && [ -f .env ]; then
    PORT="$(grep -E '^[[:space:]]*BACKEND_PORT[[:space:]]*=' .env | tail -n1 | cut -d= -f2- | xargs)"
fi
PORT="${PORT:-8000}"

if command -v uv &> /dev/null; then
    # Demo provisioning (temporary): delete backend/appdb/demo.py and
    # ui/login/demo.py to remove it.
    uv run python -m backend.appdb.demo
    uv run uvicorn backend.api:app --port "$PORT" --reload
else
    echo "uv is required to run the backend: https://docs.astral.sh/uv/" >&2
    exit 1
fi
