#!/bin/bash

# Live confidence-policy checks. Run alone because tests/conftest.py installs
# dummy provider keys before backend imports. Extra arguments go to pytest.
# Usage: bash ./confidence.sh [pytest args...]

if command -v uv &> /dev/null; then
    RUN_LIVE_CONFIDENCE=1 uv run pytest tests/test_live_confidence.py "$@"
else
    RUN_LIVE_CONFIDENCE=1 python -m pytest tests/test_live_confidence.py "$@"
fi
