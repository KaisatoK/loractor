"""Progress events that show which skill an investigation used.

Two things put a skill in front of the evidence agent, and neither reported
itself: the role playbook is pasted into the ``find_evidence`` prompt (so no tool
call marks it), and ``read_file`` / ``read_skill`` calls emit no progress. The
panel therefore showed no skill at all on a run that never read one.

Both are reported as ``tool_progress`` payloads with ``status: "skill"``; the
line shown is rendered once, in ``stream_events.progress_line``. They are labelled
differently on purpose:

* ``Playbook: <skill> (injected)`` -- the role's playbook, always present;
* ``Read skill: <skill>`` -- a skill the agent opened itself.

Pure: no I/O and no import of the graph, so the middleware and the node share it.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

from backend.pipeline.roles import ROLE_SKILLS

SKILL_OPERATION = "skill"
SKILL_STATUS = "skill"
_SKILL_FILE = "SKILL.md"


def _event(message: str) -> dict[str, Any]:
    return {
        "type": "tool_progress",
        "operation": SKILL_OPERATION,
        "status": SKILL_STATUS,
        "message": message,
    }


def injected_playbook_event(role_name: str) -> dict[str, Any]:
    """The event for the role playbook that ``find_evidence`` injects."""
    return _event(f"Playbook: {ROLE_SKILLS[role_name]} (injected)")


def read_skill_event(skill: str) -> dict[str, Any]:
    """The event for a skill the agent read itself."""
    return _event(f"Read skill: {skill}")


def skill_read_by(tool_call: dict[str, Any]) -> str | None:
    """The skill a ``read_skill`` / ``read_file`` call opens, else ``None``.

    ``read_file`` counts only when it opens ``<skill>/SKILL.md``: the backend is
    rooted at ``skills/``, so the path is ``/<skill>/SKILL.md``. Any other file
    is not a skill being read.
    """
    name = tool_call.get("name")
    args = tool_call.get("args") or {}
    if name == "read_skill":
        skill = args.get("name")
        return skill if isinstance(skill, str) and skill else None
    if name == "read_file":
        path = args.get("file_path")
        if not isinstance(path, str):
            return None
        parts = [part for part in PurePosixPath(path).parts if part != "/"]
        if len(parts) == 2 and parts[1] == _SKILL_FILE:
            return parts[0]
    return None
