#!/bin/bash

if command -v uv &> /dev/null; then
    uv run streamlit run src/ui/streamlit_app.py
else
    streamlit run src/ui/streamlit_app.py
fi
