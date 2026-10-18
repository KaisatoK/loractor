"""Transport registry for evidence-agent tools."""
from __future__ import annotations

import logging
import os

from backend.pipeline.tools.local import LOCAL_TOOLS
from backend.pipeline.tools.mcp_tools import MCP_DB_TOOLS, list_skills, read_skill
from backend.pipeline.tools.specs import agent_specs

logger = logging.getLogger(__name__)

# Spec-table order (the catalog first: every windowed tool needs a window it can
# supply), the sweep pair last. The sweep tools stay on the main agent like the
# rest: tool_progress only reaches the UI from depth <= 1, and a sweep that
# reports nothing while it runs for minutes is worse than no sweep.
LOCAL_DB_TOOLS = [LOCAL_TOOLS[spec.name] for spec in agent_specs()]


def get_db_tools() -> tuple[list, bool]:
    """Return tools and whether MCP was selected successfully.

    ``TOOLS_TRANSPORT=mcp`` is opt-in. Construction-time discovery is done by
    the first call; an unavailable server falls back to local tools so existing
    offline operation and tests remain usable.
    """
    if os.getenv("TOOLS_TRANSPORT", "local").lower() != "mcp":
        return LOCAL_DB_TOOLS, False
    try:
        from backend.pipeline.tools.mcp_bridge import get_mcp_bridge
        if not get_mcp_bridge().check():
            raise RuntimeError("MCP readiness check failed")
        return [*MCP_DB_TOOLS, read_skill, list_skills], True
    except Exception:
        logger.warning("MCP unavailable; falling back to local database tools", exc_info=True)
        return LOCAL_DB_TOOLS, False
