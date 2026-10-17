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

    max_rows_scanned: int
    """Ceiling on estimated rows read across the whole investigation.

    Tool-call count alone is a poor proxy for cost once a corpus is a billion
    rows: one careless aggregate can read more than a hundred careful ones. This
    budget is measured against the pruning estimate in ``QueryStats``, so a run
    cannot quietly scan the corpus by issuing a small number of wide queries."""

    max_promotions: int
    """How many sweep chunks this effort may index and search in full.

    Zero disables the chunk sweep entirely. Promotion is the only operation that
    builds an index, and index builds have proved to be the fragile operation on
    this host, so only the top effort levels may do it at all."""


EFFORT_SPECS: dict[str, EffortSpec] = {
    # max_tool_calls, max_hypotheses, max_iterations, max_rows_scanned, max_promotions
    "Low": EffortSpec(6, 1, 0, 25_000_000, 0),
    "Medium": EffortSpec(10, 2, 1, 100_000_000, 0),
    "High": EffortSpec(12, 3, 2, 250_000_000, 0),
    "Extreme": EffortSpec(18, 3, 3, 1_000_000_000, 2),
    "Brutal": EffortSpec(22, 3, 3, 5_000_000_000, 4),
}


def get_effort_spec(effort: str | None) -> EffortSpec:
    """Effort policy for a request; unknown/missing values fall back to Medium."""
    return EFFORT_SPECS.get(effort or "", EFFORT_SPECS["Medium"])
