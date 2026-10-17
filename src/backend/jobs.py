"""In-process registry for background chat investigations.

Generation is decoupled from the HTTP request (and from the Streamlit script run
that started it): the worker thread consumes the pipeline event stream, buffers
every event for re-attachment, and hands the finished result to a caller-supplied
``on_finish`` hook that persists the assistant message. A client may therefore
disconnect — switch conversation, delete, refresh — while the investigation keeps
running and its result still lands in the chat store.

The registry is intentionally in-memory and single-process: it matches the
current deployment (one uvicorn process, one Streamlit process). Durable,
cross-process job state would be a separate upgrade.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

RUNNING = "running"
DONE = "done"
ERROR = "error"
CANCELLED = "cancelled"

_TERMINAL = {DONE, ERROR, CANCELLED}

# Retain this many finished runs so a late/refreshed client can still replay the
# tail (result and full event log); older ones are evicted.
_MAX_FINISHED = 64

# Long-poll ceiling, so a stranded request can never block indefinitely.
_MAX_WAIT_SECONDS = 30.0


@dataclass
class RunState:
    """Live state for one investigation, shared between worker and pollers."""

    run_id: str
    user_id: str
    chat_id: str
    status: str = RUNNING
    events: list[str] = field(default_factory=list)
    error: str | None = None
    result: dict[str, Any] | None = None
    # Derived from the event stream so they can be persisted and re-rendered.
    answer: str = ""
    tool_results: list[Any] = field(default_factory=list)
    progress: list[str] = field(default_factory=list)
    # Wall-clock window from accepting the user's question to the run reaching a
    # terminal state. Monotonic, so it is immune to clock changes mid-run.
    started_at: float = field(default_factory=time.monotonic)
    elapsed_seconds: float | None = None
    cancel: threading.Event = field(default_factory=threading.Event)
    condition: threading.Condition = field(default_factory=threading.Condition)


class RunRegistry:
    """Tracks running investigations and buffers their events."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._runs: dict[str, RunState] = {}
        self._active: dict[tuple[str, str], str] = {}
        self._finished: deque[str] = deque()

    # --- lifecycle ------------------------------------------------------------

    def active_run(self, user_id: str, chat_id: str) -> str | None:
        """The run id currently investigating this chat, if any."""
        with self._lock:
            return self._active.get((user_id, chat_id))

    def start(
        self,
        *,
        user_id: str,
        chat_id: str,
        produce: Callable[[RunState], Iterator[str]],
        on_finish: Callable[[RunState], None],
    ) -> RunState:
        """Start an investigation, or return the existing one for this chat.

        ``produce`` yields the pipeline's newline-delimited JSON event strings;
        ``on_finish`` runs once the run reaches a terminal state (always, in a
        ``finally``) so the caller can persist results and release capacity.
        """
        with self._lock:
            existing = self._active.get((user_id, chat_id))
            if existing is not None:
                return self._runs[existing]
            run = RunState(
                run_id=uuid.uuid4().hex,
                user_id=user_id,
                chat_id=chat_id,
            )
            self._runs[run.run_id] = run
            self._active[(user_id, chat_id)] = run.run_id

        thread = threading.Thread(
            target=self._worker,
            args=(run, produce, on_finish),
            name=f"investigation-{run.run_id[:8]}",
            daemon=True,
        )
        thread.start()
        return run

    def get(self, run_id: str) -> RunState | None:
        return self._runs.get(run_id)

    def cancel(self, run_id: str) -> bool:
        """Request cancellation; the worker stops at its next event."""
        run = self._runs.get(run_id)
        if run is None:
            return False
        run.cancel.set()
        return True

    def discard(self, run_id: str) -> None:
        """Drop a run and its buffered events (e.g. after the chat is deleted)."""
        with self._lock:
            run = self._runs.pop(run_id, None)
            if run is not None:
                self._active.pop((run.user_id, run.chat_id), None)
            try:
                self._finished.remove(run_id)
            except ValueError:
                pass

    # --- polling --------------------------------------------------------------

    def snapshot(self, run_id: str, since: int = 0) -> dict | None:
        """Events from ``since`` plus terminal status/result, or ``None``."""
        run = self._runs.get(run_id)
        if run is None:
            return None
        with run.condition:
            events = run.events[since:]
            payload = {
                "run_id": run_id,
                "status": run.status,
                "events": [json.loads(line) for line in events],
                "next_since": len(run.events),
                "error": run.error,
                "result": run.result if run.status in _TERMINAL else None,
            }
        return payload

    def wait(self, run_id: str, since: int, timeout: float) -> None:
        """Block until there are events past ``since`` or the run terminates."""
        run = self._runs.get(run_id)
        if run is None:
            return
        timeout = max(0.0, min(timeout, _MAX_WAIT_SECONDS))
        deadline = time.monotonic() + timeout
        with run.condition:
            while (
                len(run.events) <= since
                and run.status not in _TERMINAL
                and not run.cancel.is_set()
            ):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                run.condition.wait(remaining)

    # --- worker ---------------------------------------------------------------

    def _worker(
        self,
        run: RunState,
        produce: Callable[[RunState], Iterator[str]],
        on_finish: Callable[[RunState], None],
    ) -> None:
        lines: Iterator[str] | None = None
        try:
            lines = produce(run)
            for line in lines:
                if run.cancel.is_set():
                    run.status = CANCELLED
                    break
                self._append(run, line)
            else:
                run.status = DONE
        except Exception as exc:  # noqa: BLE001 - surface any pipeline failure
            run.error = f"{type(exc).__name__}: {exc}"
            run.status = ERROR
        finally:
            if lines is not None and hasattr(lines, "close"):
                try:
                    lines.close()  # type: ignore[union-attr]
                except Exception:  # noqa: BLE001
                    pass
            run.elapsed_seconds = time.monotonic() - run.started_at
            run.result = {
                "answer": run.answer,
                "tool_results": run.tool_results,
                "progress": run.progress,
                "elapsed_seconds": round(run.elapsed_seconds, 2),
            }
            try:
                on_finish(run)
            finally:
                self._finish(run)

    def _append(self, run: RunState, line: str) -> None:
        try:
            event = json.loads(line)
        except (TypeError, ValueError):
            return
        self._track(run, event)
        with run.condition:
            run.events.append(line)
            run.condition.notify_all()

    def _track(self, run: RunState, event: dict) -> None:
        """Derive answer/tool_results/progress from an event (UI parity)."""
        etype = event.get("type")
        content = event.get("content", "")
        if etype == "messages":
            run.answer = content
        elif etype == "tool_results":
            try:
                run.tool_results = json.loads(content) or []
            except (TypeError, ValueError):
                run.tool_results = []
        elif etype == "tool_progress":
            try:
                op = json.loads(content)
            except (TypeError, ValueError):
                return
            action, status = op.get("operation", ""), op.get("status")
            if status == "completed":
                run.progress.append(f"✓ {action}")
            elif status not in ("started", None):
                run.progress.append(f"{action} — failed")
        elif etype == "subgraph_name":
            run.progress.append(f"Currently at stage: {content}")
        elif etype == "stage":
            run.progress.append(f"Currently at stage: {content}")
        elif etype == "subgraph_message":
            run.progress.append(f"Thinking: {content}")

    def _finish(self, run: RunState) -> None:
        with self._lock:
            self._active.pop((run.user_id, run.chat_id), None)
            self._finished.append(run.run_id)
            while len(self._finished) > _MAX_FINISHED:
                stale = self._finished.popleft()
                self._runs.pop(stale, None)
        with run.condition:
            run.condition.notify_all()


# Module-level registry, mirroring ``metrics`` in observability.
registry = RunRegistry()
