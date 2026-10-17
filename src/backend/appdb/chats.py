"""Persistent chat and message storage, scoped to a user."""

import json
from datetime import datetime, timezone
from typing import Any

from backend.appdb.database import db


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_chat(row) -> dict:
    return {
        "id": row["id"],
        "role": row["role"],
        "title": row["title"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _row_to_message(row) -> dict:
    return {
        "seq": row["seq"],
        "role": row["role"],
        "content": row["content"],
        "tool_results": json.loads(row["tool_results"]) if row["tool_results"] else None,
        "progress": json.loads(row["progress"]) if row["progress"] else None,
        "reframed_for": row["reframed_for"],
        "stopped": bool(row["stopped"]),
        "created_at": row["created_at"],
    }


def create_chat(user_id: str, chat_id: str, role: str, title: str | None = None) -> dict:
    now = _now()
    with db() as cur:
        cur.execute(
            "INSERT INTO chats(id, user_id, role, title, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id, id) DO UPDATE SET role=excluded.role, "
            "title=COALESCE(excluded.title, chats.title), updated_at=excluded.updated_at",
            (chat_id, user_id, role, title, now, now),
        )
        row = cur.execute(
            "SELECT id, role, title, created_at, updated_at FROM chats "
            "WHERE user_id = ? AND id = ?",
            (user_id, chat_id),
        ).fetchone()
    return _row_to_chat(row)


def list_chats(user_id: str) -> list[dict]:
    with db() as cur:
        rows = cur.execute(
            "SELECT id, role, title, created_at, updated_at FROM chats "
            "WHERE user_id = ? ORDER BY updated_at DESC",
            (user_id,),
        ).fetchall()
    return [_row_to_chat(r) for r in rows]


def delete_chat(user_id: str, chat_id: str) -> bool:
    with db() as cur:
        cur.execute(
            "DELETE FROM role_answers WHERE user_id = ? AND chat_id = ?",
            (user_id, chat_id),
        )
        cur.execute(
            "DELETE FROM messages WHERE user_id = ? AND chat_id = ?",
            (user_id, chat_id),
        )
        cur.execute(
            "DELETE FROM chats WHERE user_id = ? AND id = ?", (user_id, chat_id)
        )
        return cur.rowcount > 0


def get_messages(user_id: str, chat_id: str) -> list[dict]:
    with db() as cur:
        rows = cur.execute(
            "SELECT seq, role, content, tool_results, progress, reframed_for, "
            "stopped, created_at "
            "FROM messages WHERE user_id = ? AND chat_id = ? ORDER BY seq",
            (user_id, chat_id),
        ).fetchall()
    return [_row_to_message(r) for r in rows]


def get_chat(user_id: str, chat_id: str) -> dict | None:
    with db() as cur:
        row = cur.execute(
            "SELECT id, role, title, created_at, updated_at FROM chats "
            "WHERE user_id = ? AND id = ?",
            (user_id, chat_id),
        ).fetchone()
    return _row_to_chat(row) if row else None


# --- Per-role answers (role switch cache) -----------------------------------

# Effort levels in increasing depth; a cached answer serves any request at the
# same or a lower effort.
EFFORT_ORDER = ("Low", "Medium", "High", "Extreme", "Brutal")


def effort_rank(effort: str | None) -> int:
    try:
        return EFFORT_ORDER.index(effort or "")
    except ValueError:
        return EFFORT_ORDER.index("Medium")


def _row_to_role_answer(row) -> dict:
    return {
        "message_seq": row["message_seq"],
        "role": row["role"],
        "effort": row["effort"],
        "content": row["content"],
        "tool_results": json.loads(row["tool_results"]) if row["tool_results"] else None,
        "progress": json.loads(row["progress"]) if row["progress"] else None,
        "created_at": row["created_at"],
    }


_ROLE_ANSWER_COLUMNS = (
    "message_seq, role, effort, content, tool_results, progress, created_at"
)


def list_role_answers(user_id: str, chat_id: str) -> list[dict]:
    with db() as cur:
        rows = cur.execute(
            f"SELECT {_ROLE_ANSWER_COLUMNS} FROM role_answers "
            "WHERE user_id = ? AND chat_id = ? ORDER BY message_seq, created_at",
            (user_id, chat_id),
        ).fetchall()
    return [_row_to_role_answer(r) for r in rows]


def get_role_answer(user_id: str, chat_id: str, message_seq: int, role: str) -> dict | None:
    with db() as cur:
        row = cur.execute(
            f"SELECT {_ROLE_ANSWER_COLUMNS} FROM role_answers "
            "WHERE user_id = ? AND chat_id = ? AND message_seq = ? AND role = ?",
            (user_id, chat_id, message_seq, role),
        ).fetchone()
    return _row_to_role_answer(row) if row else None


def save_role_answer(
    user_id: str,
    chat_id: str,
    message_seq: int,
    role: str,
    effort: str,
    content: str,
    tool_results: list[Any] | None = None,
    progress: list[Any] | None = None,
) -> dict:
    """Store (or replace with a deeper run) the answer for one message and role."""
    now = _now()
    with db() as cur:
        exists = cur.execute(
            "SELECT 1 FROM chats WHERE user_id = ? AND id = ?", (user_id, chat_id)
        ).fetchone()
        if exists is None:
            raise KeyError("chat not found")
        cur.execute(
            "INSERT INTO role_answers(user_id, chat_id, message_seq, role, effort, "
            "content, tool_results, progress, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id, chat_id, message_seq, role) DO UPDATE SET "
            "effort=excluded.effort, content=excluded.content, "
            "tool_results=excluded.tool_results, progress=excluded.progress, "
            "created_at=excluded.created_at",
            (
                user_id,
                chat_id,
                message_seq,
                role,
                effort,
                content,
                json.dumps(tool_results) if tool_results is not None else None,
                json.dumps(progress) if progress is not None else None,
                now,
            ),
        )
    return get_role_answer(user_id, chat_id, message_seq, role)  # type: ignore[return-value]


def append_message(
    user_id: str,
    chat_id: str,
    role: str,
    content: str,
    tool_results: list[Any] | None = None,
    progress: list[Any] | None = None,
    reframed_for: str | None = None,
) -> dict:
    now = _now()
    with db() as cur:
        exists = cur.execute(
            "SELECT 1 FROM chats WHERE user_id = ? AND id = ?", (user_id, chat_id)
        ).fetchone()
        if exists is None:
            raise KeyError("chat not found")

        (next_seq,) = cur.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM messages "
            "WHERE user_id = ? AND chat_id = ?",
            (user_id, chat_id),
        ).fetchone()
        cur.execute(
            "INSERT INTO messages("
            "user_id, chat_id, seq, role, content, tool_results, progress, "
            "reframed_for, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                user_id,
                chat_id,
                next_seq,
                role,
                content,
                json.dumps(tool_results) if tool_results is not None else None,
                json.dumps(progress) if progress is not None else None,
                reframed_for,
                now,
            ),
        )
        cur.execute(
            "UPDATE chats SET updated_at = ? WHERE user_id = ? AND id = ?",
            (now, user_id, chat_id),
        )
    return {
        "seq": next_seq,
        "role": role,
        "content": content,
        "tool_results": tool_results,
        "progress": progress,
        "reframed_for": reframed_for,
        "stopped": False,
        "created_at": now,
    }


def mark_message_stopped(user_id: str, chat_id: str, seq: int) -> bool:
    """Flag a user message whose investigation was stopped before it answered.

    Returns ``False`` when the message no longer exists (the chat was deleted).
    """
    with db() as cur:
        cur.execute(
            "UPDATE messages SET stopped = 1 "
            "WHERE user_id = ? AND chat_id = ? AND seq = ? AND role = 'user'",
            (user_id, chat_id, seq),
        )
        return cur.rowcount > 0
