#!/bin/bash

# Live confidence-policy checks. Run alone because tests/conftest.py installs
# dummy provider keys before backend imports. Extra arguments go to pytest.
# Usage: bash ./confidence.sh [pytest args...]

if command -v uv &> /dev/null; then
    RUN_LIVE_CONFIDENCE=1 uv run pytest tests/test_live_confidence.py "$@"
else
    echo "uv is required to run the confidence tests: https://docs.astral.sh/uv/" >&2
    exit 1
fi
