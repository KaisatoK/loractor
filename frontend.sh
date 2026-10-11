#!/bin/bash

# UI port: FRONTEND_PORT from the environment or .env, default 8501.
PORT="${FRONTEND_PORT:-}"
if [ -z "$PORT" ] && [ -f .env ]; then
    PORT="$(grep -E '^[[:space:]]*FRONTEND_PORT[[:space:]]*=' .env | tail -n1 | cut -d= -f2- | xargs)"
fi
PORT="${PORT:-8501}"

if command -v uv &> /dev/null; then
    uv run streamlit run src/ui/streamlit_app.py --server.port "$PORT"
else
    streamlit run src/ui/streamlit_app.py --server.port "$PORT"
fi
