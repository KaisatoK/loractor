"""Transport registry for evidence-agent tools."""
from __future__ import annotations

import logging
import os

from backend.pipeline.tools import (
    compare_periods_tool as local_compare_periods_tool,
    count_by_category_tool as local_count_by_category_tool,
    get_data_tool as local_get_data_tool,
    get_log_by_line_id_tool as local_get_log_by_line_id_tool,
    get_trend_tool as local_get_trend_tool,
    search_logs_tool as local_search_logs_tool,
    top_in_category_by_service_tool as local_top_in_category_by_service_tool,
)
from backend.pipeline.tools.mcp_tools import MCP_DB_TOOLS, list_skills, read_skill

logger = logging.getLogger(__name__)

LOCAL_DB_TOOLS = [
    local_count_by_category_tool, local_get_data_tool, local_get_log_by_line_id_tool,
    local_get_trend_tool, local_search_logs_tool, local_top_in_category_by_service_tool,
    local_compare_periods_tool,
]


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
