from typing import Any, Literal, TypedDict

from pydantic import BaseModel, Field


class Role(TypedDict):
    name: Literal["engineer", "team lead", "manager"]
    objective: str


class NormalizedOutput(BaseModel):
    """A concise, normalized representation of the user's question."""

    normalized_question: str = Field(
        description="A concise, normalized version of the user's question."
    )
    intent: str = Field(
        description="A free-form description of the user's desired outcome."
    )
    context: dict[str, Any] = Field(
        default_factory=dict, description="Dynamic question-specific JSON."
    )
    ambiguities: list[str] = Field(
        default_factory=list, description="Uncertainties that could affect the result."
    )


class EvidenceHypothesis(BaseModel):
    hypothesis: str = Field(
        description="A plausible explanation or investigative direction for the problem."
    )
    description: str = Field(
        description="Why this hypothesis could explain the user's problem."
    )
    required_evidence: list[str] = Field(
        description="Facts that must be observed to evaluate this hypothesis."
    )


class ExecutionPlan(BaseModel):
    hypotheses: list[EvidenceHypothesis] = Field(
        description="Investigation hypotheses and the evidence required to evaluate them."
    )


class EvidenceStatus(BaseModel):
    requirement: str = Field(description="The required evidence being evaluated.")
    status: Literal["supports", "contradicts", "not_found", "unavailable"]
    source_tool: str | None = None
    observed_value: Any | None = None


class HypothesisEvidence(BaseModel):
    hypothesis: str
    status: Literal["supported", "contradicted", "unresolved"]
    evidence: list[EvidenceStatus]


class EvidenceReport(BaseModel):
    hypotheses: list[HypothesisEvidence]


class PipelineRoute(TypedDict):
    next_state: Literal["plan", "answer", "conclude"]
    pass_probability: float
    probabilities: dict[str, float]
    revision_reason: str


class PlanAttempt(TypedDict):
    plan: ExecutionPlan
    evidence: dict[str, Any]
    answer: str
    confidence: PipelineRoute | None


class PipelineState(TypedDict):
    question: str
    role: Role
    normalized: NormalizedOutput
    plan: ExecutionPlan
    plan_history: list[PlanAttempt]
    evidence: dict[str, Any]
    confidence: PipelineRoute
    revision_feedback: str
    answer: str
