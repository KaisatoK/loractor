from __future__ import annotations

import asyncio

import pytest
from fastmcp import Client

from backend.mcp_server import server
from backend.pipeline.tools.mcp_bridge import MCPBridge


def test_mcp_server_lists_tools_and_skills():
    async def run():
        async with Client(server.mcp) as client:
            names = {tool.name for tool in await client.list_tools()}
            assert {"search_logs_tool", "read_skill", "list_skills"} <= names
            assert "get_log_templates_tool" not in names
            # Auto-complete is a chat-box feature served in-process, not an
            # evidence tool, so the MCP service does not offer it.
            assert "auto_complete" not in names
            resource = await client.read_resource("skill://log-investigation/SKILL.md")
            assert "# Log Investigation" in resource[0].text
            result = await client.call_tool("read_skill", {"name": "log-investigation"})
            assert not result.is_error
            assert "hypothes" in result.content[0].text.lower()

    asyncio.run(run())


def test_mcp_server_validates_inputs():
    async def run():
        async with Client(server.mcp) as client:
            result = await client.call_tool(
                "search_logs_tool",
                {"query_text": "x", "limit": 0, "dataset": "openstack"},
                raise_on_error=False,
            )
            assert result.is_error
            result = await client.call_tool(
                "get_trend_tool",
                {"start_time": "2020-01-02 00:00:00", "end_time": "2020-01-01 00:00:00",
                 "type": ["service", "nova"], "dataset": "openstack"},
                raise_on_error=False,
            )
            assert result.is_error

    asyncio.run(run())


def test_mcp_server_rejects_unknown_datasets():
    """The model selects a corpus id, never a table: an unregistered id -- such
    as one of the old per-file table names -- is rejected by validation."""
    async def run():
        async with Client(server.mcp) as client:
            result = await client.call_tool(
                "search_logs_tool",
                {"query_text": "timeout", "dataset": "openstack_normal1"},
                raise_on_error=False,
            )
            assert result.is_error
            result = await client.call_tool(
                "count_by_category_tool",
                {"start_time": "2020-01-01 00:00:00", "end_time": "2020-01-02 00:00:00",
                 "dataset": "t"},
                raise_on_error=False,
            )
            assert result.is_error

    asyncio.run(run())

def test_sync_bridge_uses_one_loop_and_preserves_skill_result(monkeypatch):
    monkeypatch.setattr(server, "_SKILLS_ROOT", server._SKILLS_ROOT)
    bridge = MCPBridge(target=server.mcp, timeout=10)
    try:
        assert bridge.call_tool("read_skill", {"name": "log-investigation"}).startswith("#")
        thread_name = bridge._thread.name
        assert bridge.call_tool("list_skills", {})[0]["name"] == "log-investigation"
        assert bridge._thread.name == thread_name
    finally:
        bridge.close()


def test_skill_path_is_sandboxed():
    with pytest.raises(Exception):
        server.read_skill("../README")
    with pytest.raises(Exception):
        server.read_skill("/etc/passwd")
