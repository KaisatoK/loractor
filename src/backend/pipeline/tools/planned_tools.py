"""Planned evidence tool (docs/tool_requests.md T3): signature only.

The argument schema and the description shared by every transport are in
``tools/schemas.py`` (``CountByTeam`` / ``COUNT_BY_TEAM_DESCRIPTION``). The
result kind is in ``envelope.TOOL_KINDS`` and the classifier entry in
``prompts.TOOL_CATALOG``.

Not yet wired into a transport: when implementing, decorate it with
``@tool(args_schema=..., description=...)``, build the envelope with
``execute_database_tool``, add the DB query in ``backend.db``, then register it
in ``registry.LOCAL_DB_TOOLS``, ``mcp_tools.MCP_DB_TOOLS`` and
``mcp_server/server.py`` (see AGENTS.md "Integrating new tools and tables").
``context_tools.get_nearby_events_tool`` is a finished example of the pattern.
"""

from langchain.tools import ToolRuntime


def count_by_team_tool(
    runtime: ToolRuntime,
    datasets: list[str] | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    category: str | None = None,
) -> dict:
    """T3: log-entry counts per owning team and severity (kind ``aggregate``)."""
    pass
