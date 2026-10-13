from dataclasses import dataclass
from typing import Literal

EffortLevel = Literal["Low", "Medium", "High", "Extreme", "Brutal"]


@dataclass(frozen=True)
class EffortSpec:
    max_tool_calls: int  # active: find_evidence ToolCallLimitMiddleware run_limit
    max_hypotheses: int  # active: per-turn prompt budget (schema hard cap stays 3)
    max_iterations: int  # active: confidence revision-loop routing cap (max rollbacks)


EFFORT_SPECS: dict[str, EffortSpec] = {
    "Low": EffortSpec(3, 1, 0),
    "Medium": EffortSpec(5, 2, 1),
    "High": EffortSpec(8, 3, 2),
    "Extreme": EffortSpec(13, 3, 3),
    "Brutal": EffortSpec(21, 3, 3),
}


def get_effort_spec(effort: str | None) -> EffortSpec:
    """Effort policy for a request; unknown/missing values fall back to Medium."""
    return EFFORT_SPECS.get(effort or "", EFFORT_SPECS["Medium"])
