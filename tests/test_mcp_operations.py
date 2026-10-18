"""M7: lifespan, logging without payloads, rate limiting and the one cached tool."""

from __future__ import annotations

import asyncio
import logging

import pytest
from fastmcp import Client, FastMCP

from backend.db import connection
from backend.mcp_server import config, operations


def _server(monkeypatch, *, rps=50.0, ttl=300) -> tuple[FastMCP, dict]:
    monkeypatch.setattr(config, "MCP_RATE_LIMIT_RPS", rps)
    monkeypatch.setattr(config, "MCP_CACHE_TTL_S", ttl)
    calls = {"count_by_dataset_tool": 0, "list_datasets_tool": 0}
    server = FastMCP("ops", middleware=operations.middleware())

    @server.tool
    def count_by_dataset_tool() -> dict:
        calls["count_by_dataset_tool"] += 1
        return {"n": calls["count_by_dataset_tool"]}

    @server.tool
    def list_datasets_tool() -> dict:
        calls["list_datasets_tool"] += 1
        return {"n": calls["list_datasets_tool"]}

    return server, calls


def _call_n(server, name, times):
    async def go():
        async with Client(server) as client:
            return [
                (await client.call_tool(name, {}, raise_on_error=False))
                for _ in range(times)
            ]

    return asyncio.run(go())


def test_only_count_by_dataset_is_cached(monkeypatch):
    server, calls = _server(monkeypatch)
    counted = _call_n(server, "count_by_dataset_tool", 3)
    listed = _call_n(server, "list_datasets_tool", 3)
    assert calls == {"count_by_dataset_tool": 1, "list_datasets_tool": 3}
    assert {r.structured_content["n"] for r in counted} == {1}
    assert [r.structured_content["n"] for r in listed] == [1, 2, 3]


def test_caching_can_be_turned_off(monkeypatch):
    server, calls = _server(monkeypatch, ttl=0)
    _call_n(server, "count_by_dataset_tool", 3)
    assert calls["count_by_dataset_tool"] == 3


def test_a_client_over_its_rate_is_refused_not_the_server_broken(monkeypatch):
    from mcp.shared.exceptions import MCPError

    server, calls = _server(monkeypatch, rps=5.0, ttl=0)
    outcomes = []

    async def go():
        async with Client(server) as client:
            for _ in range(40):
                try:
                    await client.call_tool("list_datasets_tool", {})
                    outcomes.append("ok")
                except MCPError as error:
                    outcomes.append(str(error))

    asyncio.run(go())
    assert any("Rate limit exceeded" in o for o in outcomes), outcomes
    assert "ok" in outcomes  # the first requests, inside the burst, were served
    assert calls["list_datasets_tool"] < 40


def test_rate_limiting_can_be_turned_off(monkeypatch):
    server, calls = _server(monkeypatch, rps=0, ttl=0)
    results = _call_n(server, "list_datasets_tool", 30)
    assert not any(r.is_error for r in results)


def test_client_id_is_a_digest_never_the_token(monkeypatch):
    class Token:
        token = "super-secret-bearer"

    monkeypatch.setattr(operations, "get_access_token", lambda: Token())
    ident = operations.client_id(None)
    assert ident.startswith("token:") and "super-secret" not in ident
    assert ident == operations.client_id(None)
    monkeypatch.setattr(operations, "get_access_token", lambda: None)
    assert operations.client_id(None) == "anonymous"


def test_request_logs_carry_no_arguments(monkeypatch, caplog):
    server, _ = _server(monkeypatch, ttl=0)

    @server.tool
    def echo(secret: str) -> str:
        return "ok"

    async def go():
        async with Client(server) as client:
            await client.call_tool("echo", {"secret": "LOG-TEXT-PAYLOAD"})

    with caplog.at_level(logging.INFO):
        asyncio.run(go())
    assert any(r.name == "backend.mcp_server.requests" for r in caplog.records)
    assert "LOG-TEXT-PAYLOAD" not in caplog.text


def test_the_pool_opens_and_closes_with_the_server(monkeypatch):
    events = []
    monkeypatch.setattr(connection, "warm_pool", lambda: events.append("open"))
    monkeypatch.setattr(connection, "close_pool", lambda: events.append("close"))
    server = FastMCP("life", lifespan=operations.server_lifespan)

    async def go():
        async with Client(server):
            events.append("serving")

    asyncio.run(go())
    assert events == ["open", "serving", "close"]


def test_a_database_that_is_down_at_start_does_not_stop_the_server(monkeypatch):
    def down():
        raise OSError("connection refused")

    monkeypatch.setattr(connection, "warm_pool", down)
    monkeypatch.setattr(connection, "close_pool", lambda: None)
    server = FastMCP("life", lifespan=operations.server_lifespan)

    @server.tool
    def ping() -> str:
        return "pong"

    async def go():
        async with Client(server) as client:
            return (await client.call_tool("ping", {})).data

    assert asyncio.run(go()) == "pong"


def test_close_pool_is_safe_without_a_pool_and_rebuilds_lazily(monkeypatch):
    class FakePool:
        closed = False

        def closeall(self):
            FakePool.closed = True

    monkeypatch.setattr(connection, "_pool", FakePool())
    connection.close_pool()
    assert FakePool.closed and connection._pool is None
    connection.close_pool()  # nothing to close: still fine


def test_the_real_server_has_the_stack_installed():
    from backend.mcp_server.app import mcp

    kinds = {type(m).__name__ for m in mcp.middleware}
    assert {
        "StructuredLoggingMiddleware",
        "TimingMiddleware",
        "RateLimitingMiddleware",
        "ResponseCachingMiddleware",
    } <= kinds
