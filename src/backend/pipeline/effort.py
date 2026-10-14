from dataclasses import dataclass
from typing import Literal

EffortLevel = Literal["Low", "Medium", "High", "Extreme", "Brutal"]


@dataclass(frozen=True)
class EffortSpec:
    max_tool_calls: int
    """The single agent's ``ToolCallLimitMiddleware`` ``run_limit`` — a hard cap
    on ALL tool calls in one investigation run, including to-do writes."""

    max_hypotheses: int
    """Per-turn prompt budget (schema hard cap stays 3)."""

    max_iterations: int
    """Confidence revision-loop cap consumed by ``decision.decide_confidence``
    (the maximum number of rollbacks)."""

EFFORT_SPECS: dict[str, EffortSpec] = {
    "Low": EffortSpec(4, 1, 0),
    "Medium": EffortSpec(8, 2, 1),
    "High": EffortSpec(10, 3, 2),
    "Extreme": EffortSpec(16, 3, 3),
    "Brutal": EffortSpec(20, 3, 3),
}


def get_effort_spec(effort: str | None) -> EffortSpec:
    """Effort policy for a request; unknown/missing values fall back to Medium."""
    return EFFORT_SPECS.get(effort or "", EFFORT_SPECS["Medium"])
