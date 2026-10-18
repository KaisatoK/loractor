"""Synchronous access to the asynchronous FastMCP client.

The graph intentionally remains synchronous. One daemon event-loop thread owns
MCP client lifecycle and worker threads submit calls with
``run_coroutine_threadsafe``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import queue
import re
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Any

from fastmcp import Client

from backend.mcp_server.config import MCP_AUTH_TOKEN, MCP_CALL_TIMEOUT_S, MCP_URL
from backend.observability import metrics

logger = logging.getLogger(__name__)

# How often a waiting caller wakes to hand over queued progress events.
_POLL_S = 0.2
# The server sends "[stage] message".
_STAGE_RE = re.compile(r"^\[(\w+)\]\s*(.*)$", re.DOTALL)


class MCPBridge:
    def __init__(self, target: Any = None, timeout: float = MCP_CALL_TIMEOUT_S) -> None:
        self.target = target or MCP_URL
        self.timeout = timeout
        self._thread: threading.Thread | None = None
        self._client: Client | None = None
        self._lock: asyncio.Lock | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ready = threading.Event()
        self._start_lock = threading.Lock()

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._ready.set()
        loop.run_forever()
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._start_lock:
            if self._thread is None or not self._thread.is_alive():
                self._ready.clear()
                self._thread = threading.Thread(
                    target=self._run_loop, name="mcp-bridge-loop", daemon=True
                )
                self._thread.start()
        if not self._ready.wait(self.timeout):
            raise TimeoutError("MCP bridge event loop did not start")
        assert self._loop is not None
        return self._loop

    async def _connected(self) -> Client:
        """The one client for this bridge, opened on first use.

        It lives on the loop thread for the bridge's whole life, so a call reuses
        the session instead of connecting and initialising per call. Calls from
        several worker threads share it (the protocol multiplexes requests).
        """
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            if self._client is None or not self._client.is_connected():
                auth = (
                    MCP_AUTH_TOKEN
                    if isinstance(self.target, str) and MCP_AUTH_TOKEN
                    else None
                )
                client = Client(self.target, auth=auth, timeout=self.timeout)
                await client.__aenter__()
                self._client = client
            return self._client

    async def _drop_client(self, client: Client) -> None:
        """Forget a client whose connection failed, so the next call reconnects."""
        async with self._lock:
            if self._client is client:
                self._client = None
        try:
            await client.close()
        except Exception:  # noqa: BLE001 - it was already broken
            logger.debug("closing a failed MCP client raised", exc_info=True)

    async def _call(
        self,
        name: str,
        arguments: dict[str, Any],
        timeout: float | None = None,
        on_progress: Callable[[float, float | None, str | None], None] | None = None,
    ) -> Any:
        client = await self._connected()

        async def handler(progress: float, total: float | None, message: str | None):
            if on_progress is not None:
                on_progress(progress, total, message)

        try:
            result = await client.call_tool(
                name,
                arguments,
                raise_on_error=False,
                timeout=timeout or self.timeout,
                progress_handler=handler if on_progress is not None else None,
            )
        except (asyncio.TimeoutError, asyncio.CancelledError):
            raise  # the session is fine; this one call took too long
        except Exception:
            await self._drop_client(client)
            raise
        if getattr(result, "is_error", False):
            text = "\n".join(getattr(block, "text", "") for block in result.content)
            raise RuntimeError(text or f"MCP tool {name} failed")
        structured = getattr(result, "structured_content", None)
        if structured is not None:
            if isinstance(structured, dict) and set(structured) == {"result"}:
                return structured["result"]
            return structured
        for block in getattr(result, "content", []):
            text = getattr(block, "text", None)
            if text:
                try:
                    value = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    return value
        raise RuntimeError(f"MCP tool {name} returned no structured result")

    def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        timeout: float | None = None,
        progress: Callable[..., None] | None = None,
    ) -> Any:
        """Call a tool and wait up to ``timeout`` seconds (default: the bridge's).

        The same limit bounds the client request and the wait, so a long call is
        not cut short by either one. ``progress`` receives the server's progress
        notifications as ``progress(message, stage=...)``; it is always called on
        the calling thread (events are queued by the loop thread and handed over
        here), because the pipeline's stream writer belongs to that thread.
        """
        timeout = timeout or self.timeout
        events: queue.Queue = queue.Queue()
        loop = self._ensure_loop()
        future: Future[dict[str, Any]] = asyncio.run_coroutine_threadsafe(
            self._call(
                name,
                arguments,
                timeout,
                (lambda p, t, m: events.put(m)) if progress is not None else None,
            ),
            loop,
        )
        deadline = time.monotonic() + timeout
        try:
            while True:
                self._drain(events, progress)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    future.cancel()
                    raise TimeoutError(f"MCP tool {name} did not finish in {timeout:g}s")
                try:
                    result = future.result(timeout=min(_POLL_S, remaining))
                    break
                except FutureTimeout:
                    continue
            self._drain(events, progress)
        except Exception:
            metrics.inc("mcp_failures_total")
            raise
        metrics.inc("mcp_calls_total")
        return result

    @staticmethod
    def _drain(events: queue.Queue, progress: Callable[..., None] | None) -> None:
        while progress is not None:
            try:
                message = events.get_nowait()
            except queue.Empty:
                return
            text = message or ""
            match = _STAGE_RE.match(text)
            stage, text = (match.group(1), match.group(2)) if match else ("working", text)
            try:
                progress(text, stage=stage)
            except Exception:  # noqa: BLE001 - a display problem must not fail the call
                logger.debug("progress callback failed", exc_info=True)

    def check(self) -> bool:
        try:
            self.call_tool("list_skills", {})
            return True
        except Exception:
            logger.warning("MCP readiness check failed", exc_info=True)
            return False

    def close(self) -> None:
        if self._loop and self._loop.is_running():
            if self._client is not None:
                try:
                    asyncio.run_coroutine_threadsafe(
                        self._client.close(), self._loop
                    ).result(timeout=2)
                except Exception:  # noqa: BLE001 - shutting down anyway
                    logger.debug("closing the MCP client raised", exc_info=True)
                self._client = None
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)


_bridge = MCPBridge()


def get_mcp_bridge() -> MCPBridge:
    return _bridge


def reset_mcp_bridge(bridge: MCPBridge | None = None) -> None:
    global _bridge
    _bridge.close()
    _bridge = bridge or MCPBridge()
