#!/bin/bash

# Live claim-check tests (Jev judges whether the cited results back a measured
# claim). Run alone because tests/conftest.py installs dummy provider keys before
# backend imports. Extra arguments go to pytest.
# Usage: bash ./claims.sh [pytest args...]

if command -v uv &> /dev/null; then
    RUN_LIVE_CLAIMS=1 uv run pytest tests/test_live_claims.py "$@"
else
    RUN_LIVE_CLAIMS=1 python -m pytest tests/test_live_claims.py "$@"
fi
