"""HTTP client for the backend chat API."""

import json
import os
from collections.abc import Iterator

import requests

# Backend port from the environment (.env via ui/__init__), default 8000.
BACKEND_PORT = os.environ.get("BACKEND_PORT", "8000")
API_BASE = f"http://localhost:{BACKEND_PORT}"
API_URL = f"{API_BASE}/chat/stream"
REFRAME_URL = f"{API_BASE}/chat/reframe"


def stream_chat(
    *,
    user_id: str,
    chat_id: str,
    role: str,
    effort: str,
    history: list[dict],
) -> Iterator[dict]:
    """Stream chat events from the backend as decoded dicts."""
    with requests.post(
        API_URL,
        json={
            "user_id": user_id,
            "chat_id": chat_id,
            "role": role,
            "effort": effort,
            "history": history,
        },
        stream=True,
    ) as response:
        response.raise_for_status()
        for chunk in response.iter_content(chunk_size=None, decode_unicode=True):
            if chunk:
                text = chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk
                yield json.loads(text)


def reframe(
    *,
    target_role: str,
    effort: str,
    question: str,
    previous_answer: str,
    tool_results: list,
) -> str:
    """Re-explain a completed investigation for another role (one-shot)."""
    response = requests.post(
        REFRAME_URL,
        json={
            "target_role": target_role,
            "effort": effort,
            "question": question,
            "previous_answer": previous_answer,
            "tool_results": tool_results,
        },
        timeout=120,
    )
    response.raise_for_status()
    return response.json()["answer"]
