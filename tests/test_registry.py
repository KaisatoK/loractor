"""Offline tests for the evidence-tool transport registry.

Local and MCP transports must expose the same evidence tool names: the seven
bounded query tools plus the two sweep tools.
"""

from backend.pipeline.tools.mcp_tools import MCP_DB_TOOLS
from backend.pipeline.tools.registry import LOCAL_DB_TOOLS, get_db_tools

EXPECTED_TOOLS = {
    "count_by_category_tool",
    "get_data_tool",
    "get_log_by_line_id_tool",
    "get_trend_tool",
    "search_logs_tool",
    "top_in_category_by_service_tool",
    "compare_periods_tool",
    "sweep_chunks_tool",
    "investigate_chunk_tool",
}


def test_local_and_mcp_tool_sets_match():
    local = {tool.name for tool in LOCAL_DB_TOOLS}
    remote = {tool.name for tool in MCP_DB_TOOLS}
    assert local == remote == EXPECTED_TOOLS
    assert len(LOCAL_DB_TOOLS) == len(MCP_DB_TOOLS) == 9


def test_local_transport_is_default(monkeypatch):
    monkeypatch.delenv("TOOLS_TRANSPORT", raising=False)
    tools, mcp_enabled = get_db_tools()
    assert mcp_enabled is False
    assert len(tools) == 9
