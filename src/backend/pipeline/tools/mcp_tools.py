"""LangChain tool wrappers around MCP calls.

Generated from the spec table, like the local tools: a wrapper has the spec's
name, description and schema, so the agent sees the same tool on either
transport. The wrapper restores ``ToolRuntime`` progress events and keeps the
server's whole result envelope for result collection.
"""
from __future__ import annotations

from langchain.tools import ToolRuntime, tool
from langchain_core.tools import BaseTool, StructuredTool
from pydantic_core import to_jsonable_python

from backend.mcp_server.config import MCP_SWEEP_TIMEOUT_S
from backend.pipeline.tools.executor import execute_database_tool
from backend.pipeline.tools.mcp_bridge import get_mcp_bridge
from backend.pipeline.tools.specs import ToolSpec, agent_specs


def _call(runtime: ToolRuntime, spec: ToolSpec, args: dict) -> dict:
    """Invoke an MCP tool and keep the server's whole envelope.

    The executed query, filters, truncation flag and ``query_stats`` survive,
    and a refusal arriving over MCP is passed through as a refusal (not data),
    so the agent sees the same actionable result on both transports.
    ``execute_database_tool`` fills any field an older server omits with ``None``.
    """
    arguments = to_jsonable_python(args)

    # A spec with no timeout is unbounded on the server (the sweep), so the bridge
    # must not cut it off at the short default. Other tools pass no timeout.
    options = {"timeout": MCP_SWEEP_TIMEOUT_S} if spec.timeout is None else {}

    def invoke(reporter=None):
        extra = {"progress": reporter} if reporter is not None else {}
        return get_mcp_bridge().call_tool(spec.name, arguments, **options, **extra)

    # A tool that reports from inside its call (the sweep) gets the same reporter
    # as on the local transport, fed by the server's progress notifications, so
    # the UI shows its stages under TOOLS_TRANSPORT=mcp too.
    return execute_database_tool(
        runtime=runtime,
        operation=spec.operation(args),
        function=invoke,
        with_reporter=spec.reports_progress,
        kind=spec.kind,
        dataset=spec.dataset_of(args),
        limit=spec.limit_of(args),
    )


def make_mcp_tool(spec: ToolSpec) -> BaseTool:
    """The wrapper that calls ``spec`` on the MCP server.

    A tool without model arguments passes no ``args_schema`` on purpose (see
    ``local.make_local_tool``).
    """
    if spec.args_model is None:

        def func(runtime: ToolRuntime) -> dict:
            return _call(runtime, spec, {})

    else:

        def func(runtime: ToolRuntime, **kwargs) -> dict:
            return _call(runtime, spec, kwargs)

    func.__name__ = spec.name
    return StructuredTool.from_function(
        func=func,
        name=spec.name,
        description=spec.description,
        args_schema=spec.args_model,
    )


@tool
def read_skill(name: str) -> str:
    """Read an investigation skill exposed by the MCP server."""
    result = get_mcp_bridge().call_tool("read_skill", {"name": name})
    return result.get("data", result) if isinstance(result, dict) else result


@tool
def list_skills() -> str:
    """List skills exposed by the MCP server."""
    result = get_mcp_bridge().call_tool("list_skills", {})
    return result.get("data", result) if isinstance(result, dict) else result


MCP_TOOLS: dict[str, BaseTool] = {
    spec.name: make_mcp_tool(spec) for spec in agent_specs()
}
MCP_DB_TOOLS = list(MCP_TOOLS.values())
