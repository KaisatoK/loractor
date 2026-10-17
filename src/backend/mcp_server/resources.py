"""MCP resources: the skills, and the rules the answers are held to.

Nothing here holds text of its own. Each resource is rendered, when it is read,
from the module that owns the fact (the dataset registry, the role and effort
tables, the priority formula, the result envelope, the ownership CSV), so what
an external agent reads is what the pipeline applies.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from fastmcp.server.providers.skills import SkillsDirectoryProvider

from backend.db.datasets import catalog
from backend.mcp_server.app import mcp
from backend.mcp_server.skills import _SKILLS_ROOT
from backend.pipeline import priority
from backend.pipeline.effort import EFFORT_SPECS
from backend.pipeline.playbooks import ownership_context
from backend.pipeline.roles import OVERVIEW_ROLES, ROLE_OBJECTIVES, ROLE_SKILLS
from backend.pipeline.tools.envelope import (
    MEASURED_KINDS,
    RESULT_KINDS,
    ToolEnvelope,
)

# The skills: skill://<name>/SKILL.md and skill://<name>/_manifest. Path screening
# (a skill file must resolve inside its skill's directory) is the provider's.
mcp.add_provider(SkillsDirectoryProvider(roots=_SKILLS_ROOT))


def _json(value: Any) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False, default=str)


@mcp.resource(
    "logs://datasets/{dataset_id}/schema",
    title="Dataset schema",
    mime_type="application/json",
)
def dataset_schema(dataset_id: str) -> str:
    """What one dataset holds and supports: its description and kind, approximate
    size, canonical fields, severity vocabulary (in the corpus' own casing), and
    capabilities. Never a table or column name. list_datasets_tool measures the
    live row counts and time span."""
    entries = catalog()
    if dataset_id not in entries:
        raise ValueError(
            f"Unknown dataset '{dataset_id}'. Choose one of: {', '.join(entries)}."
        )
    return _json({"dataset": dataset_id, **entries[dataset_id]})


@mcp.resource(
    "logs://ownership", title="Team ownership", mime_type="text/plain"
)
def ownership() -> str:
    """Which team owns each component. The mapping is fictional (invented for the
    challenge) and is the only source of owning teams: name a team from it, never
    from guesswork. A component not listed is owned by 'unowned'."""
    return ownership_context()


@mcp.resource(
    "logs://roles/{role}", title="Role", mime_type="application/json"
)
def role(role: str) -> str:
    """One role's objective, the skill holding its evidence plan, and whether a
    dataset-wide overview is a valid starting scope for it."""
    if role not in ROLE_SKILLS:
        raise ValueError(f"Unknown role '{role}'. Choose one of: {', '.join(ROLE_SKILLS)}.")
    return _json({
        "role": role,
        "objective": ROLE_OBJECTIVES[role],
        "skill": ROLE_SKILLS[role],
        "overview_is_a_valid_scope": role in OVERVIEW_ROLES,
    })


@mcp.resource(
    "logs://policy/result-contract",
    title="Result contract",
    mime_type="application/json",
)
def result_contract() -> str:
    """The shape of every evidence tool's result, and which kinds are frequencies."""
    return _json({
        "result_kinds": list(RESULT_KINDS),
        "measured_kinds": sorted(MEASURED_KINDS),
        "envelope": ToolEnvelope.model_json_schema(),
    })


@mcp.resource(
    "logs://policy/priority-score",
    title="Priority score",
    mime_type="application/json",
)
def priority_score() -> str:
    """How problems are ranked for a manager: a deterministic score over measured
    counts. Compute it from query results; never estimate it."""
    return _json({
        "formula": priority.FORMULA,
        "weights": priority.WEIGHTS,
        "informational_levels": sorted(priority.INFO_LEVELS),
        "severe_levels": sorted(priority.SEVERE_LEVELS),
    })


@mcp.resource(
    "logs://policy/effort", title="Effort levels", mime_type="application/json"
)
def effort_levels() -> str:
    """The investigation budget at each effort level: tool calls, hypotheses,
    rows scanned, and how many sweep chunks may be read in full."""
    return _json({level: asdict(spec) for level, spec in EFFORT_SPECS.items()})

