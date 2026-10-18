"""M7: the pipeline's bridge keeps one MCP session and relays server progress."""

import threading

from fastmcp import Context, FastMCP

from backend.pipeline.tools.mcp_bridge import MCPBridge


def _server() -> FastMCP:
    server = FastMCP("t")

    @server.tool
    async def stages(ctx: Context) -> dict:
        await ctx.report_progress(1, None, "[map] Mapped 3 chunks")
        await ctx.report_progress(2, None, "no stage prefix here")
        return {"ok": True}

    @server.tool
    async def plain() -> dict:
        return {"ok": True}

    return server


def test_calls_reuse_one_client():
    bridge = MCPBridge(target=_server(), timeout=10)
    try:
        bridge.call_tool("plain", {})
        first = bridge._client
        assert first is not None
        bridge.call_tool("plain", {})
        bridge.call_tool("stages", {})
        assert bridge._client is first
    finally:
        bridge.close()


def test_server_progress_reaches_the_callback_on_the_calling_thread():
    bridge = MCPBridge(target=_server(), timeout=10)
    seen = []

    def progress(message, *, stage="working"):
        seen.append((message, stage, threading.current_thread()))

    try:
        assert bridge.call_tool("stages", {}, progress=progress) == {"ok": True}
    finally:
        bridge.close()
    assert [(m, s) for m, s, _ in seen] == [
        ("Mapped 3 chunks", "map"),
        ("no stage prefix here", "working"),
    ]
    assert {t for *_, t in seen} == {threading.current_thread()}


def test_a_failing_callback_does_not_fail_the_call():
    bridge = MCPBridge(target=_server(), timeout=10)

    def broken(message, *, stage="working"):
        raise RuntimeError("display problem")

    try:
        assert bridge.call_tool("stages", {}, progress=broken) == {"ok": True}
    finally:
        bridge.close()


def test_a_dropped_session_reconnects_on_the_next_call():
    bridge = MCPBridge(target=_server(), timeout=10)
    try:
        bridge.call_tool("plain", {})
        old = bridge._client
        # Break the session the way a restarted server would.
        import asyncio

        asyncio.run_coroutine_threadsafe(old.close(), bridge._loop).result(5)
        assert bridge.call_tool("plain", {}) == {"ok": True}
        assert bridge._client is not old
    finally:
        bridge.close()


def test_concurrent_calls_share_the_session():
    bridge = MCPBridge(target=_server(), timeout=10)
    results = []
    try:
        threads = [
            threading.Thread(target=lambda: results.append(bridge.call_tool("plain", {})))
            for _ in range(8)
        ]
        [t.start() for t in threads]
        [t.join(15) for t in threads]
    finally:
        bridge.close()
    assert results == [{"ok": True}] * 8
