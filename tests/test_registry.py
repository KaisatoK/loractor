"""Offline tests for the evidence-tool transport registry.

Local and MCP transports must expose the same evidence tool names: the dataset
catalog, whole-dataset counts, the seven bounded query tools and the two sweep
tools.
"""

from backend.pipeline.tools.mcp_tools import MCP_DB_TOOLS
from backend.pipeline.tools.registry import LOCAL_DB_TOOLS, get_db_tools

EXPECTED_TOOLS = {
    "list_datasets_tool",
    "count_by_category_tool",
    "count_by_dataset_tool",
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
    assert len(LOCAL_DB_TOOLS) == len(MCP_DB_TOOLS) == 11


def test_local_transport_is_default(monkeypatch):
    monkeypatch.delenv("TOOLS_TRANSPORT", raising=False)
    tools, mcp_enabled = get_db_tools()
    assert mcp_enabled is False
    assert len(tools) == 11


def test_no_tool_has_an_empty_args_schema():
    """langchain-core calls a tool whose args_schema has no fields with no
    arguments at all, dropping the injected runtime: list_datasets_tool failed
    every real call with "missing 1 required positional argument: 'runtime'".
    A tool without model arguments must let its schema be inferred instead."""
    for tool in [*LOCAL_DB_TOOLS, *MCP_DB_TOOLS]:
        assert tool.args_schema.model_fields, tool.name
        assert "runtime" in tool._injected_args_keys, tool.name
        assert "runtime" not in tool.tool_call_schema.model_fields, tool.name
