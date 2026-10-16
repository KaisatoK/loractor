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
    # Routing decisions produced by the scope/confidence TypeSafe classifiers
    # (see decision.py and graph.py). The scope node picks one of four
    # action-oriented routes:
    #   clarify = ask the user first; general = conversational/not about logs;
    #   direct_answer = answer from evidence already in the conversation;
    #   find_evidence = retrieve new log evidence.
    scope: Literal["clarify", "general", "direct_answer", "find_evidence"]
    confidence: PipelineRoute
    evidence_report_history: list[EvidenceReport]
    normalized: NormalizedOutput
    evidence_report: EvidenceReport
    revision_feedback: str
    answer: str
    # Display-only raw tool rows: {id, tool_name, arguments, row_count, rows}.
    tool_results: list[dict[str, Any]]
    # The answer agent's drafted optional follow-up question.
    follow_up: str | None
    # Consecutive clarification turns, used to break clarification loops.
    clarify_count: int
    # Iterations spent in the confidence revision loop within the current turn.
    revision_count: int
    fail_reason: str | None = None
