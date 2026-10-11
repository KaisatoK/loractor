from typing import Annotated, Any, Literal, TypedDict
from pydantic import BaseModel, Field


class Role(TypedDict):
    name: Literal["engineer", "team_lead", "manager"]
    objective: str


class NormalizedOutput(BaseModel):
    """A concise, normalized representation of the user's question."""

    normalized_question: Annotated[
        str, Field(description="A concise, normalized version of the user's question.")
    ]
    intent: Annotated[
        str, Field(description="A free-form description of the user's desired outcome.")
    ]
    context: Annotated[
        dict[str, Any],
        Field(
            description=(
                "Dynamic question-specific JSON."
                "This may include important information from all conversation history "
                "that is relevant to the latest question, including entities, constraints, "
                "preferences, decisions, examples, and technical details."
            )
        ),
    ]
    ambiguities: Annotated[
        list[str], Field(description="Uncertainties that could affect the result.")
    ]


class PipelineRoute(TypedDict):
    next_state: Literal["find_evidence", "answer", "end"]
    pass_probability: float
    probabilities: dict[str, float]
    revision_reason: str


class ToolStatus(BaseModel):
    name: str = Field(description="The exact name of the tool invoked.")
    parameters: dict[str, Any] = Field(
        description="The parameters supplied to the tool invocation."
    )
    status: Literal["success", "failure"] = Field(
        description="Whether the tool invocation completed successfully."
    )
    error_message: str | None = Field(
        default=None,
        description=(
            "The error returned by a failed invocation. Must be null when status "
            "is 'success'."
        ),
    )


class Evidence(BaseModel):
    name: str = Field(description="The name of the evidence item.")
    description: str = Field(
        description="A description of why this evidence is needed and what it evaluates."
    )
    tool_used: list[ToolStatus] = Field(
        description="A list of tools used to collect the evidence item."
    )
    availability: Literal["available", "unavailable"] = Field(
        description=("Whether the required evidence was successfully obtained.")
    )


class Hypothesis(BaseModel):
    hypothesis: str = Field(
        description=(
            "A specific, testable explanation that can be supported or refuted "
            "by collected evidence."
        )
    )
    description: str = Field(
        description=(
            "Why the hypothesis is plausible and relevant to the reported problem."
        )
    )
    required_evidence: list[Evidence] = Field(
        description=(
            "The evidence required to evaluate the hypothesis, including the "
            "findings and collection attempts."
        ),
    )
    status: Literal["supported", "unsupported", "inconclusive"] = Field(
        description=(
            "The conclusion based only on collected evidence. Use 'inconclusive' "
            "when evidence is missing, insufficient, ambiguous, or conflicting."
        )
    )


class EvidenceReport(BaseModel):
    hypotheses: list[Hypothesis] = Field(
        max_length=3,
        description=(
            "The investigated hypotheses and the evidence-based conclusion for each. "
            "Must be non-duplicative, plausible, and testable. Maximum of 3 hypotheses."
        ),
    )


class PipelineState(TypedDict):
    conversation: str
    latest_message: str
    latest_answer: str
    role: Role
    effort: Literal["Low", "Medium", "High", "Extreme", "Brutal"]
    scope: Literal["irrelevant", "resolved", "unresolved"]
    normalized: NormalizedOutput
    evidence_report: EvidenceReport
    evidence_report_history: list[EvidenceReport]
    confidence: PipelineRoute
    revision_feedback: str
    answer: str
    # Display-only raw tool rows: {id, tool_name, arguments, row_count, rows}.
    tool_results: list[dict[str, Any]]
    fail_reason: str | None = None
