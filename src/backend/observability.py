"""Request correlation and lightweight in-process metrics.
No external dependency: counters live in memory, exposed as JSON at
``/metrics``; a pure-ASGI middleware attaches a request id to logs and headers."""

import contextvars
import logging
import threading
import time
from collections import Counter
from uuid import uuid4

logger = logging.getLogger("backend.access")

_request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "request_id", default=None
)


def set_request_id(rid: str):
    return _request_id.set(rid)


def reset_request_id(token) -> None:
    _request_id.reset(token)


def current_request_id() -> str | None:
    return _request_id.get()


class Metrics:
    """Thread-safe counter registry with a start time."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: Counter[str] = Counter()
        self._started_at = time.time()

    def inc(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self._counters[name] += amount

    def snapshot(self) -> dict:
        with self._lock:
            counters = dict(self._counters)
        return {
            "uptime_seconds": round(time.time() - self._started_at, 1),
            "counters": counters,
        }


metrics = Metrics()


class RequestContextMiddleware:
    """Pure-ASGI middleware: request id + structured access log.
    Raw ASGI (not ``BaseHTTPMiddleware``) so streamed response bodies pass
    through untouched."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = dict(scope.get("headers") or [])
        header_rid = incoming.get(b"x-request-id", b"").decode("latin-1").strip()
        rid = header_rid or uuid4().hex
        token = set_request_id(rid)
        start = time.perf_counter()
        status = {"code": 0}

        async def send_with_context(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
                headers = list(message.get("headers") or [])
                headers.append((b"x-request-id", rid.encode("latin-1")))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_with_context)
        finally:
            metrics.inc("http_requests_total")
            elapsed_ms = (time.perf_counter() - start) * 1000
            logger.info(
                "%s %s -> %s (%.1fms) [rid=%s]",
                scope.get("method"),
                scope.get("path"),
                status["code"] or "-",
                elapsed_ms,
                rid,
            )
            reset_request_id(token)
