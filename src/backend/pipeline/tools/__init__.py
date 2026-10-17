"""Database-backed tools exposed to the evidence agent.

Every tool is declared once, in :mod:`backend.pipeline.tools.specs`; the local
tools are generated from that table and exported here under their own names.
"""

from backend.pipeline.tools.local import LOCAL_TOOLS
from backend.pipeline.tools.specs import SPECS

globals().update(LOCAL_TOOLS)

# Every declared tool (planned ones are declared but not generated).
__all__ = sorted(spec.name for spec in SPECS)
