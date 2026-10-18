#!/bin/bash

# Profile OTel query latency with SereneDB EXPLAIN ANALYZE.
# Reports the server-side Total Time for each OTel query shape.
# Extra arguments are passed to the Python module.
# Usage: bash ./profile_otel.sh [--runs 3] [--window-seconds 5]

if command -v uv &> /dev/null; then
    uv run python -m backend.db.otel_latency "$@"
else
    python -m backend.db.otel_latency "$@"
fi
