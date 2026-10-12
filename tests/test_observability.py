"""Tests for request correlation and the metrics registry."""

from backend.observability import (
    Metrics,
    RequestContextMiddleware,
    current_request_id,
    reset_request_id,
    set_request_id,
)


def test_counters_accumulate_and_snapshot():
    metrics = Metrics()
    metrics.inc("requests")
    metrics.inc("requests", 2)
    metrics.inc("errors")
    snapshot = metrics.snapshot()
    assert snapshot["counters"] == {"requests": 3, "errors": 1}
    assert snapshot["uptime_seconds"] >= 0


def test_request_id_contextvar_lifecycle():
    assert current_request_id() is None
    token = set_request_id("rid-1")
    assert current_request_id() == "rid-1"
    reset_request_id(token)
    assert current_request_id() is None


def test_middleware_adds_and_echoes_request_id():
    import anyio

    captured = {}

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def run(headers):
        sent = []

        async def send(message):
            sent.append(message)

        middleware = RequestContextMiddleware(app)
        await middleware(
            {"type": "http", "method": "GET", "path": "/x", "headers": headers},
            None,
            send,
        )
        captured["sent"] = sent

    anyio.run(run, [])
    headers = dict(captured["sent"][0]["headers"])
    assert headers[b"x-request-id"]

    anyio.run(run, [(b"x-request-id", b"trace-9")])
    headers = dict(captured["sent"][0]["headers"])
    assert headers[b"x-request-id"] == b"trace-9"
