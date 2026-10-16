"""Synchronous access to the asynchronous FastMCP client.

The graph intentionally remains synchronous. One daemon event-loop thread owns
MCP client lifecycle and worker threads submit calls with
``run_coroutine_threadsafe``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from concurrent.futures import Future
from datetime import timedelta
from typing import Any

from fastmcp import Client

from backend.mcp_server.config import MCP_AUTH_TOKEN, MCP_CALL_TIMEOUT_S, MCP_URL
from backend.observability import metrics

logger = logging.getLogger(__name__)


class MCPBridge:
    def __init__(self, target: Any = None, timeout: float = MCP_CALL_TIMEOUT_S) -> None:
        self.target = target or MCP_URL
        self.timeout = timeout
        self._thread: threading.Thread | None = None
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

    async def _call(self, name: str, arguments: dict[str, Any]) -> Any:
        auth = MCP_AUTH_TOKEN if isinstance(self.target, str) and MCP_AUTH_TOKEN else None
        client = Client(self.target, auth=auth, timeout=self.timeout)
        async with client:
            result = await client.call_tool(name, arguments, raise_on_error=False)
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

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        loop = self._ensure_loop()
        future: Future[dict[str, Any]] = asyncio.run_coroutine_threadsafe(
            self._call(name, arguments), loop
        )
        try:
            result = future.result(timeout=self.timeout)
        except Exception:
            metrics.inc("mcp_failures_total")
            raise
        metrics.inc("mcp_calls_total")
        return result

    def check(self) -> bool:
        try:
            self.call_tool("list_skills", {})
            return True
        except Exception:
            logger.warning("MCP readiness check failed", exc_info=True)
            return False

    def close(self) -> None:
        if self._loop and self._loop.is_running():
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
