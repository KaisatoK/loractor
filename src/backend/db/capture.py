"""Capture the SQL text each tool call actually executed.

The tool-result contract ("show its work") needs the exact query that ran, with
its parameters bound. ``query_runner`` records every executed statement into the
list opened by :func:`capture_queries`; outside such a block nothing is recorded,
so one-off tooling and tests that call query functions directly are unaffected.

A ``ContextVar`` keeps concurrent tool calls (worker threads, asyncio tasks)
from seeing each other's statements.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_captured: ContextVar[list[str] | None] = ContextVar("captured_queries", default=None)


def _compact(statement: str) -> str:
    """Drop blank lines and indentation so the stored SQL stays readable."""
    lines = (line.strip() for line in statement.splitlines())
    return "\n".join(line for line in lines if line)


def record_query(statement: bytes | str | None) -> None:
    """Append one executed statement to the active capture, if any."""
    target = _captured.get()
    if target is None or not statement:
        return
    if isinstance(statement, bytes):
        statement = statement.decode("utf-8", errors="replace")
    target.append(_compact(statement))


@contextmanager
def capture_queries() -> Iterator[list[str]]:
    """Collect every statement ``query_runner`` executes inside the block."""
    captured: list[str] = []
    token = _captured.set(captured)
    try:
        yield captured
    finally:
        _captured.reset(token)
