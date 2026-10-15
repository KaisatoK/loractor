#!/bin/bash

# Live scope-route tests: replay each scripted conversation against the real
# normalizer and scope classifier (two API calls per asserted turn) and write the
# HTML report to .data/scope_routes_report.html (path printed once per run). Run
# from the repo root; API keys are read from .env. Extra arguments are passed
# through to pytest.
# Usage: bash ./routes.sh [pytest args...]

if command -v uv &> /dev/null; then
    RUN_LIVE_ROUTES=1 uv run pytest tests/test_live_scope_routes.py "$@"
else
    echo "uv is required to run the route tests: https://docs.astral.sh/uv/" >&2
    exit 1
fi
