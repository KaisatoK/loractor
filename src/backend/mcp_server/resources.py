"""MCP resources: the investigation skills."""

from __future__ import annotations

from backend.mcp_server.app import mcp
from backend.mcp_server.skills import read_skill_text, skill_files


@mcp.resource("skill://{name}")
def skill_resource(name: str) -> str:
    """Expose a skill as a read-only MCP resource."""
    return read_skill_text(name)


# Register concrete URIs as well as the template so clients can discover every
# available skill through ``resources/list`` without guessing names.
def _register_concrete_skill_resources() -> None:
    def make(skill_name: str):
        def resource() -> str:
            return read_skill_text(skill_name)

        return resource

    for skill_name in skill_files():
        mcp.resource(f"skill://{skill_name}")(make(skill_name))


_register_concrete_skill_resources()
