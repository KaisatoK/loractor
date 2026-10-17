"""Planned evidence tools (docs/tool_requests.md T2, T3): signatures only.

The argument schemas and the description shared by every transport are in
``tools/schemas.py`` (``GetNearbyEvents`` / ``NEARBY_EVENTS_DESCRIPTION`` and
``CountByTeam`` / ``COUNT_BY_TEAM_DESCRIPTION``). The result kinds are in
``envelope.TOOL_KINDS`` and the classifier entries in ``prompts.TOOL_CATALOG``.

Not yet wired into a transport: when implementing, decorate each with
``@tool(args_schema=..., description=...)``, build the envelope with
``execute_database_tool``, add the DB query in ``backend.db``, then register it
in ``registry.LOCAL_DB_TOOLS``, ``mcp_tools.MCP_DB_TOOLS`` and
``mcp_server/server.py`` (see AGENTS.md "Integrating new tools and tables").
"""

from langchain.tools import ToolRuntime

from backend.db.datasets import DEFAULT_DATASET_ID


def get_nearby_events_tool(
    line_id: int,
    runtime: ToolRuntime,
    dataset: str = DEFAULT_DATASET_ID,
    before: int = 5,
    after: int = 5,
    same_entity: bool = False,
    same_request: bool = False,
) -> dict:
    """T2: log entries around an anchor line (kind ``context``)."""
    pass


def count_by_team_tool(
    runtime: ToolRuntime,
    datasets: list[str] | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    category: str | None = None,
) -> dict:
    """T3: log-entry counts per owning team and severity (kind ``aggregate``)."""
    pass
