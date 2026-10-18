"""Only the sweep tools may outlive the MCP call timeout on the pipeline's bridge."""

import asyncio

import pytest
from fastmcp import FastMCP

from backend.mcp_server.config import MCP_CALL_TIMEOUT_S, MCP_SWEEP_TIMEOUT_S
from backend.pipeline.tools import mcp_tools
from backend.pipeline.tools.mcp_bridge import MCPBridge
from backend.pipeline.tools.specs import BY_NAME, SPECS


def _slow_server() -> FastMCP:
    server = FastMCP("slow")

    @server.tool
    async def slow() -> dict:
        await asyncio.sleep(0.6)
        return {"done": True}

    return server


def test_a_call_past_the_bridge_timeout_fails():
    bridge = MCPBridge(target=_slow_server(), timeout=0.2)
    try:
        with pytest.raises(Exception):
            bridge.call_tool("slow", {})
    finally:
        bridge.close()


def test_a_longer_per_call_timeout_lets_the_same_call_finish():
    bridge = MCPBridge(target=_slow_server(), timeout=0.2)
    try:
        assert bridge.call_tool("slow", {}, timeout=10) == {"done": True}
    finally:
        bridge.close()


def test_sweep_timeout_is_longer_than_the_default():
    assert MCP_SWEEP_TIMEOUT_S > MCP_CALL_TIMEOUT_S


def test_only_unbounded_specs_ask_for_the_long_timeout(monkeypatch):
    seen = {}

    class _Bridge:
        def call_tool(self, name, arguments, **options):
            seen[name] = options
            return {}

    monkeypatch.setattr(mcp_tools, "get_mcp_bridge", lambda: _Bridge())
    monkeypatch.setattr(mcp_tools, "execute_database_tool", lambda **kw: kw["function"]())

    # Every tool that can run for minutes (the sweep, and the ones that build an
    # index on demand) is unbounded on the server and waits long on the bridge.
    unbounded = {spec.name for spec in SPECS if spec.timeout is None}
    assert {"sweep_chunks_tool", "investigate_chunk_tool"} <= unbounded
    assert "list_datasets_tool" not in unbounded

    # One bounded tool and the two unbounded ones, each with the arguments its
    # operation label reads.
    sweep_args = {"dataset": "otel_s3", "question": "q", "confirm_cost": False,
                  "max_promotions": 0, "start_at": 0}
    mcp_tools._call(None, BY_NAME["list_datasets_tool"], {})
    mcp_tools._call(None, BY_NAME["sweep_chunks_tool"], sweep_args)
    mcp_tools._call(None, BY_NAME["investigate_chunk_tool"],
                    {"dataset": "otel_s3", "chunk_id": "part_000:1-1", "question": "q"})

    assert seen["list_datasets_tool"] == {}
    assert seen["sweep_chunks_tool"] == {"timeout": MCP_SWEEP_TIMEOUT_S}
    assert seen["investigate_chunk_tool"] == {"timeout": MCP_SWEEP_TIMEOUT_S}


def test_the_sweep_wrapper_hands_the_bridge_a_progress_reporter(monkeypatch):
    seen = {}

    class _Bridge:
        def call_tool(self, name, arguments, **options):
            seen[name] = options
            return {}

    def fake_execute(**kw):
        reporter = object()
        return kw["function"](reporter) if kw["with_reporter"] else kw["function"]()

    monkeypatch.setattr(mcp_tools, "get_mcp_bridge", lambda: _Bridge())
    monkeypatch.setattr(mcp_tools, "execute_database_tool", fake_execute)

    mcp_tools._call(None, BY_NAME["list_datasets_tool"], {})
    mcp_tools._call(
        None,
        BY_NAME["sweep_chunks_tool"],
        {"dataset": "otel_s3", "question": "q", "confirm_cost": False,
         "max_promotions": 0, "start_at": 0},
    )
    assert "progress" not in seen["list_datasets_tool"]
    assert "progress" in seen["sweep_chunks_tool"]
