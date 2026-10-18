"""Server-side limits on the expensive tools, owned by the deployment.

A tool's argument bounds are the same for every transport (``schemas.py``); what an
operator allows an external agent to spend is a server setting. The sweep's
promotions are the costly part (about eleven minutes each), so
``MCP_MAX_PROMOTIONS`` caps them. A call over the cap is *refused*, with the limit
and what to do instead, rather than failing: the agent can follow it and retry.
"""

from __future__ import annotations

from typing import Any

from backend.db.errors import refuse
from backend.mcp_server import config
from backend.pipeline.tools.specs import ToolSpec


def enforce_limits(spec: ToolSpec, args: dict[str, Any]) -> None:
    """Raise a refusal when ``args`` ask for more than this server allows."""
    cap = config.MCP_MAX_PROMOTIONS
    if spec.name == "sweep_chunks_tool":
        wanted = int(args.get("max_promotions") or 0)
    elif spec.name == "investigate_chunk_tool":
        wanted = 1  # reading one chunk is one promotion
    else:
        return
    if wanted <= cap:
        return
    if spec.name == "investigate_chunk_tool":
        reason = "This server allows no promotions (MCP_MAX_PROMOTIONS=0), and reading a chunk is one."
        suggestion = (
            "Use sweep_chunks_tool with max_promotions=0 to rank chunks without "
            "reading message text, or ask the operator to raise MCP_MAX_PROMOTIONS."
        )
    else:
        reason = (
            f"max_promotions={wanted} is above this server's limit of {cap} "
            "(MCP_MAX_PROMOTIONS); each promotion costs about eleven minutes."
        )
        suggestion = (
            f"Retry with max_promotions={cap} or lower"
            + ("; 0 ranks chunks without reading any message text." if cap else ".")
        )
    raise refuse(
        reason,
        suggestion,
        code="promotion_limit",
        requested=wanted,
        limit=cap,
    )
