"""LangGraph state for the chat pipeline."""

from typing import Any, Literal, TypedDict

from backend.pipeline.contracts import EvidenceReport, NormalizedOutput
from backend.pipeline.effort import EffortLevel

RoleName = Literal["engineer", "team_lead", "manager"]


class Role(TypedDict):
    name: RoleName
    objective: str


class PipelineRoute(TypedDict):
    next_state: Literal["find_evidence", "answer", "end"]
    pass_probability: float
    probabilities: dict[str, float]
    revision_reason: str


class PipelineState(TypedDict):
    conversation: str
    latest_message: str
    latest_answer: str
    role: Role
    effort: EffortLevel
    # The next three fields are driven by the scope/confidence nodes, which are
    # currently documented stubs (see graph.py). They stay in the schema so the
    # TypeSafe routing logic can be re-enabled without a state migration.
    scope: Literal["irrelevant", "resolved", "unresolved"]
    confidence: PipelineRoute
    # Reserved: per-turn evidence history for revision loops, unused today.
    evidence_report_history: list[EvidenceReport]
    normalized: NormalizedOutput
    evidence_report: EvidenceReport
    revision_feedback: str
    answer: str
    # Display-only raw tool rows: {id, tool_name, arguments, row_count, rows}.
    tool_results: list[dict[str, Any]]
    fail_reason: str | None = None
