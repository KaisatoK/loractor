#!/bin/bash

cd src/backend/db

if command -v uv &> /dev/null; then
    uv run setup.py
else
    py -m setup
fi
