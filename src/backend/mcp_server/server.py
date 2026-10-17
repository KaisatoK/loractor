"""FastMCP server for the chatbot's data and investigation skills.

The server owns the SereneDB connection pool. Every database tool is generated
from the spec table (``backend.pipeline.tools.specs``) and returns the
tool-result envelope defined in ``backend.pipeline.tools.envelope`` (``kind``,
``dataset``, executed ``query``, ``filters``, ``limit``, ``returned``,
``truncated``, ``data`` and, when measured, ``query_stats``), the same contract
as the in-process tools, so the pipeline can switch transports without changing
its evidence contract and an external agent (Claude) can show its work. A query
the layer refuses (unbounded, too wide, unsupported on that corpus) comes back
as ``status: "refused"`` with a ``reason`` and a ``suggestion``, not an error.

Tools take a ``dataset`` id from a closed vocabulary (``backend.db.datasets``),
never a table name, and rows use the canonical field names (``ts``,
``severity``, ``service``, ``entity_id``, ``message``, ``template``,
``event_id``, ``record_id``) whatever the corpus calls them.

To add a tool, declare it in the spec table; see AGENTS.md "Integrating new
tools and tables". Importing this module registers everything on ``mcp``.
"""

from __future__ import annotations

from backend.mcp_server import resources as _resources  # noqa: F401  (registers)
from backend.mcp_server import tools as _tools  # noqa: F401  (registers)
from backend.mcp_server.app import mcp
from backend.mcp_server.skills import _SKILLS_ROOT  # noqa: F401
from backend.mcp_server.tools import list_skills, read_skill

__all__ = ["list_skills", "mcp", "read_skill"]
