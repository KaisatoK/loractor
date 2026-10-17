#!/bin/bash

# Worked investigation: put one topic to the same pipeline as engineer, team lead
# and manager (live provider calls + a live, seeded SereneDB) and write
# docs/worked_investigation.md from the real tool calls, queries and answers.
# Run from the repo root; API keys are read from .env. Extra arguments are passed
# to the generator.
# Usage: bash ./worked_investigation.sh --topic "<issue or pattern>" [--effort High]
#        [--engineer-question "..."] [--team-lead-question "..."] [--manager-question "..."]

if command -v uv &> /dev/null; then
    CHECKPOINT_BACKEND=memory uv run python -m backend.worked_investigation "$@"
else
    echo "uv is required to run the worked investigation: https://docs.astral.sh/uv/" >&2
    exit 1
fi
