"""HTTP client for the backend API (auth, chats, and chat streaming)."""

import json
import os
from collections.abc import Iterator

import requests

# Backend port from the environment (.env via ui/__init__), default 8000.
BACKEND_PORT = os.environ.get("BACKEND_PORT", "8000")
API_BASE = f"http://localhost:{BACKEND_PORT}"
STREAM_URL = f"{API_BASE}/chat/stream"
REFRAME_URL = f"{API_BASE}/chat/reframe"


def _auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# --- Authentication -----------------------------------------------------------


def register(username: str, password: str) -> dict:
    response = requests.post(
        f"{API_BASE}/auth/register",
        json={"username": username, "password": password},
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def login(username: str, password: str) -> dict:
    response = requests.post(
        f"{API_BASE}/auth/login",
        json={"username": username, "password": password},
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def logout(token: str) -> None:
    requests.post(f"{API_BASE}/auth/logout", headers=_auth_headers(token), timeout=10)


# --- Chats --------------------------------------------------------------------


def list_chats(token: str) -> list:
    response = requests.get(
        f"{API_BASE}/chats", headers=_auth_headers(token), timeout=30
    )
    response.raise_for_status()
    return response.json()["chats"]


def create_chat(token: str, chat_id: str, role: str, title: str | None = None) -> dict:
    response = requests.post(
        f"{API_BASE}/chats",
        json={"chat_id": chat_id, "role": role, "title": title},
        headers=_auth_headers(token),
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def delete_chat(token: str, chat_id: str) -> None:
    response = requests.delete(
        f"{API_BASE}/chats/{chat_id}", headers=_auth_headers(token), timeout=30
    )
    response.raise_for_status()


def get_messages(token: str, chat_id: str) -> list:
    response = requests.get(
        f"{API_BASE}/chats/{chat_id}/messages",
        headers=_auth_headers(token),
        timeout=30,
    )
    response.raise_for_status()
    return response.json()["messages"]


def auto_complete(token: str, query: str) -> list[dict]:
    """Type-ahead suggestions from the log index's field values.
    A leading selector in ``query`` scopes it to one field: ``@`` component,
    ``^`` event template, ``$`` content. No dataset is sent, so the backend
    merges every local corpus; a ``$`` suggestion carries its ``dataset``."""
    response = requests.get(
        f"{API_BASE}/auto-complete",
        params={"query": query},
        headers=_auth_headers(token),
        timeout=30,
    )
    response.raise_for_status()
    return response.json()["suggestions"]


def append_message(
    token: str,
    chat_id: str,
    *,
    role: str,
    content: str,
    tool_results: list | None = None,
    progress: list | None = None,
    reframed_for: str | None = None,
) -> None:
    response = requests.post(
        f"{API_BASE}/chats/{chat_id}/messages",
        json={
            "role": role,
            "content": content,
            "tool_results": tool_results,
            "progress": progress,
            "reframed_for": reframed_for,
        },
        headers=_auth_headers(token),
        timeout=30,
    )
    response.raise_for_status()


# --- Chat streaming -----------------------------------------------------------


def stream_chat(
    *,
    token: str,
    chat_id: str,
    role: str,
    effort: str,
    history: list[dict],
) -> Iterator[dict]:
    """Stream chat events from the backend as decoded dicts.
    Retained for compatibility; the UI now uses ``start_investigation``.
    """
    with requests.post(
        STREAM_URL,
        json={
            "chat_id": chat_id,
            "role": role,
            "effort": effort,
            "history": history,
        },
        headers=_auth_headers(token),
        stream=True,
        timeout=600,
    ) as response:
        response.raise_for_status()
        # The backend emits newline-delimited JSON; split on lines so events are
        # parsed correctly regardless of how TCP/HTTP chunks them.
        for line in response.iter_lines(decode_unicode=True):
            if line:
                yield json.loads(line)


# --- Background investigations --------------------------------------------------


def start_investigation(
    *,
    token: str,
    chat_id: str,
    role: str,
    effort: str,
    question: str,
    history: list[dict],
) -> dict:
    """Start a backend-owned investigation; returns ``{run_id, status}``."""
    response = requests.post(
        f"{API_BASE}/chats/{chat_id}/investigate",
        json={
            "role": role,
            "effort": effort,
            "question": question,
            "history": history,
        },
        headers=_auth_headers(token),
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def get_run(
    *,
    token: str,
    chat_id: str,
    run_id: str,
    since: int = 0,
    wait: float = 0.0,
) -> dict:
    """Fetch buffered run events from ``since`` (optionally long-polling)."""
    response = requests.get(
        f"{API_BASE}/chats/{chat_id}/run/{run_id}",
        params={"since": since, "wait": wait},
        headers=_auth_headers(token),
        timeout=(wait + 30) if wait else 30,
    )
    response.raise_for_status()
    return response.json()


def active_run(*, token: str, chat_id: str) -> str | None:
    """The run id currently investigating this chat, if any."""
    response = requests.get(
        f"{API_BASE}/chats/{chat_id}/active-run",
        headers=_auth_headers(token),
        timeout=30,
    )
    response.raise_for_status()
    return response.json().get("run_id")


def cancel_run(*, token: str, chat_id: str, run_id: str) -> bool:
    """Ask the backend to stop an in-flight investigation.

    Returns whether the backend accepted the request. The run does not stop at
    once: it ends at its next event, so poll the run until it reports
    ``cancelled``. Raises on an HTTP error (for example 404 for an unknown run).
    """
    response = requests.post(
        f"{API_BASE}/chats/{chat_id}/run/{run_id}/cancel",
        headers=_auth_headers(token),
        timeout=30,
    )
    response.raise_for_status()
    return bool(response.json().get("cancelled"))


# --- Role switch -----------------------------------------------------------------


def request_role_answer(
    *, token: str, chat_id: str, message_seq: int, role: str, effort: str
) -> dict:
    """Ask for an earlier answer as another role.

    Returns ``{"status": "cached", "answer": {...}}`` when that role's answer is
    already stored at this effort or deeper, else ``{"status", "run_id"}`` for a
    new background investigation (poll it with ``get_run``).
    """
    response = requests.post(
        f"{API_BASE}/chats/{chat_id}/messages/{message_seq}/role-answer",
        json={"role": role, "effort": effort},
        headers=_auth_headers(token),
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def list_role_answers(token: str, chat_id: str) -> list[dict]:
    """Every cached per-role answer of a chat."""
    response = requests.get(
        f"{API_BASE}/chats/{chat_id}/role-answers",
        headers=_auth_headers(token),
        timeout=30,
    )
    response.raise_for_status()
    return response.json()["answers"]


def reframe(
    *,
    token: str,
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
        headers=_auth_headers(token),
        timeout=120,
    )
    response.raise_for_status()
    return response.json()["answer"]
