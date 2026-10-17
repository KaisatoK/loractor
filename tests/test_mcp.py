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
            resource = await client.read_resource("skill://log-investigation")
            assert resource[0].text.startswith("# Log Investigation")
            result = await client.call_tool("read_skill", {"name": "log-investigation"})
            assert not result.is_error
            assert "hypothes" in result.content[0].text.lower()

    asyncio.run(run())


def test_mcp_server_serves_nearby_events_and_validates_it():
    async def run():
        async with Client(server.mcp) as client:
            listed = {tool.name: tool for tool in await client.list_tools()}
            tool = listed["get_nearby_events_tool"]
            assert "offset" in tool.description and "same_request" in tool.description
            assert {"line_id", "same_entity", "same_request"} <= set(
                tool.inputSchema["properties"]
            )
            for bad in ({"line_id": 0}, {"line_id": 1, "before": 51},
                        {"line_id": 1, "dataset": "t"}):
                result = await client.call_tool(
                    "get_nearby_events_tool", bad, raise_on_error=False
                )
                assert result.is_error, bad

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


def test_mcp_server_serves_count_by_team_and_validates_it():
    async def run():
        async with Client(server.mcp) as client:
            listed = {tool.name: tool for tool in await client.list_tools()}
            tool = listed["count_by_team_tool"]
            assert "ownership table" in tool.description
            assert {"datasets", "start_time", "end_time", "category"} <= set(
                tool.inputSchema["properties"]
            )
            for bad in (
                {"datasets": ["otel_s3"]},
                {"start_time": "2017-05-16 00:00:00"},
                {
                    "start_time": "2017-05-17 00:00:00",
                    "end_time": "2017-05-16 00:00:00",
                },
            ):
                result = await client.call_tool(
                    "count_by_team_tool", bad, raise_on_error=False
                )
                assert result.is_error, bad

    asyncio.run(run())


def test_local_mcp_and_server_share_team_description():
    from backend.mcp_server import server
    from backend.pipeline.tools import count_by_team_tool
    from backend.pipeline.tools.mcp_tools import count_by_team_tool as mcp_tool
    from backend.pipeline.tools.schemas import COUNT_BY_TEAM_DESCRIPTION

    assert count_by_team_tool.description == COUNT_BY_TEAM_DESCRIPTION
    assert mcp_tool.description == COUNT_BY_TEAM_DESCRIPTION
    assert server.count_by_team_tool.description == COUNT_BY_TEAM_DESCRIPTION
