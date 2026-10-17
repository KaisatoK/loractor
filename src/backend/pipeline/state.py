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
    revision_reason: str
    # Raw Noul yes-probabilities used by the code-owned revision policy.
    signals: dict[str, float]


class PipelineState(TypedDict):
    conversation: str
    # Structured raw transcript for semantic classifiers; conversation remains
    # the rendered string consumed by the text-oriented agents.
    messages: list[dict[str, str]]
    latest_message: str
    latest_answer: str
    role: Role
    effort: EffortLevel
    # Routing route from the scope classifier (decision.py/graph.py): clarify =
    # ask first; general = conversational/not logs; direct_answer = answer from
    # evidence already present; find_evidence = retrieve new log evidence.
    scope: Literal["clarify", "general", "direct_answer", "find_evidence"]
    confidence: PipelineRoute
    evidence_report_history: list[EvidenceReport]
    normalized: NormalizedOutput
    evidence_report: EvidenceReport
    revision_feedback: str
    answer: str
    # Display-only raw tool rows: {id, tool_name, arguments, row_count, rows}.
    tool_results: list[dict[str, Any]]
    # Every database-tool call of the evidence stage (also failed and empty
    # ones): {call_id, tool_name, arguments, status, error_message, result_ids}.
    tool_calls: list[dict[str, Any]]
    # The answer agent's drafted optional follow-up question.
    follow_up: str | None
    # Consecutive clarification turns, used to break clarification loops.
    clarify_count: int
    # Raw scope Noul probabilities of this turn, kept for tracing a route.
    scope_signals: dict[str, float]
    # Why this turn was not scoped, derived in code (decision.clarify_reasons);
    # the clarifier asks about exactly these.
    clarify_reasons: list[str]
    # Dataset ids the investigation was restricted to because the clarify budget
    # ran out without a scope (decision.scope_assumption); None otherwise. The
    # answer states it in a code-rendered line.
    assumed_scope: list[str] | None
    # Iterations spent in the confidence revision loop within the current turn.
    revision_count: int
    # Set only by a role-switch run (its own graph thread): skips the scope
    # classifier and routes straight to this scope.
    forced_scope: Literal["find_evidence"] | None
    fail_reason: str | None = None
