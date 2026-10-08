#!/bin/bash

cd src

if command -v uv &> /dev/null; then
    uv run uvicorn backend.api:app --reload
else
    uvicorn backend.api:app --reload
fi