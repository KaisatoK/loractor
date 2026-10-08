from typing import Annotated, Any, Literal, TypedDict

from pydantic import BaseModel, Field


TABLE_NAME = "log"
DATETIME_REGEX = r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$"


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
        dict[str, Any], Field(
            description=(
                "Dynamic question-specific JSON."
                "This may include important information from all conversation history "
                "that is relevant to the latest question, including entities, constraints, "
                "preferences, decisions, examples, and technical details."
            )
        )
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
    description: str = Field(description="A description of why this evidence is needed and what it evaluates.")
    tool_used: list[ToolStatus] = Field(
        description="A list of tools used to collect the evidence item."
    )
    availability: Literal["available", "unavailable"] = Field(
        description=(
            "Whether the required evidence was successfully obtained."
        )
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
        description=(
            "The investigated hypotheses and the evidence-based conclusion for each."
        ),
    )

class PipelineState(TypedDict):
    conversation: str
    role: Role
    effort: Literal["Low", "Medium", "High", "Extreme", "Brutal"]
    scope: Literal["irrelevant", "resolved", "unresolved"]
    normalized: NormalizedOutput
    evidence_report: EvidenceReport
    evidence_report_history: list[EvidenceReport]
    confidence: PipelineRoute
    revision_feedback: str
    answer: str


class CountByCategory(BaseModel):
    start_time: Annotated[
        str,
        Field(
            pattern=DATETIME_REGEX,
            description="Timestamp in YYYY-MM-DD HH:MM:SS format.",
        ),
    ]
    end_time: Annotated[
        str,
        Field(
            pattern=DATETIME_REGEX,
            description="Timestamp in YYYY-MM-DD HH:MM:SS format.",
        ),
    ]
    table_name: str = TABLE_NAME
    category: str | None = None


class GetTrendPID(BaseModel):
    start_time: Annotated[
        str,
        Field(
            pattern=DATETIME_REGEX,
            description="Timestamp in YYYY-MM-DD HH:MM:SS format.",
        ),
    ]
    end_time: Annotated[
        str,
        Field(
            pattern=DATETIME_REGEX,
            description="Timestamp in YYYY-MM-DD HH:MM:SS format.",
        ),
    ]
    pid: Annotated[
        int,
        Field(
            ge=0,
            description="Exact process ID. The value must come from the user or from previously collected evidence.",
        ),
    ]
    table_name: str = TABLE_NAME
    category: str | None = None


class GetTrendService(BaseModel):
    start_time: Annotated[
        str,
        Field(
            pattern=DATETIME_REGEX,
            description="Timestamp in YYYY-MM-DD HH:MM:SS format.",
        ),
    ]
    end_time: Annotated[
        str,
        Field(
            pattern=DATETIME_REGEX,
            description="Timestamp in YYYY-MM-DD HH:MM:SS format.",
        ),
    ]
    service: Annotated[
        str,
        Field(
            min_length=1,
            description="Exact service name. The value must come from the user or from previously collected evidence.",
        ),
    ]
    table_name: str = TABLE_NAME
    category: str | None = None


class GetTrendEvent(BaseModel):
    start_time: Annotated[
        str,
        Field(
            pattern=DATETIME_REGEX,
            description="Timestamp in YYYY-MM-DD HH:MM:SS format.",
        ),
    ]
    end_time: Annotated[
        str,
        Field(
            pattern=DATETIME_REGEX,
            description="Timestamp in YYYY-MM-DD HH:MM:SS format.",
        ),
    ]
    event_id: Annotated[
        str,
        Field(
            min_length=1,
            description="Exact event ID. The value must come from the user or from previously collected evidence.",
        ),
    ]
    table_name: str = TABLE_NAME
    category: str | None = None


class SearchLogs(BaseModel):
    query_text: Annotated[
        str,
        Field(
            min_length=1,
            max_length=500,
            description=(
                "Text or concept to search for in log messages. "
                "Do not include INFO, WARNING, ERROR (or any case variations like info, Warning, Err) "
                "merely as severity filters. Do not include empty strings, whitespace-only strings, or strings that are only punctuation. "
            ),
        ),
    ]
    category: str | None = None
    table_name: str = TABLE_NAME
    limit: Annotated[
        int,
        Field(
            ge=1,
            le=1000,
            description="Maximum number of log entries to return. Must be between 1 and 1000.",
        ),
    ] = 10


class TopInCategoryByService(BaseModel):
    start_time: Annotated[
        str,
        Field(
            pattern=DATETIME_REGEX,
            description="Timestamp in YYYY-MM-DD HH:MM:SS format.",
        ),
    ]
    end_time: Annotated[
        str,
        Field(
            pattern=DATETIME_REGEX,
            description="Timestamp in YYYY-MM-DD HH:MM:SS format.",
        ),
    ]
    table_name: str = TABLE_NAME
    category: str | None = None
    limit: Annotated[
        int,
        Field(
            ge=1,
            le=100,
            description="Number of top services to return. Must be between 1 and 100.",
        ),
    ] = 5


class ComparePeriods(BaseModel):
    baseline_start: Annotated[
        str,
        Field(
            pattern=DATETIME_REGEX,
            description="Timestamp in YYYY-MM-DD HH:MM:SS format.",
        ),
    ]
    baseline_end: Annotated[
        str,
        Field(
            pattern=DATETIME_REGEX,
            description="Timestamp in YYYY-MM-DD HH:MM:SS format.",
        ),
    ]
    recent_start: Annotated[
        str,
        Field(
            pattern=DATETIME_REGEX,
            description="Timestamp in YYYY-MM-DD HH:MM:SS format.",
        ),
    ]
    recent_end: Annotated[
        str,
        Field(
            pattern=DATETIME_REGEX,
            description="Timestamp in YYYY-MM-DD HH:MM:SS format.",
        ),
    ]
    table_name: str = TABLE_NAME
    limit: Annotated[
        int,
        Field(
            ge=1,
            le=100,
            description="Maximum number of log entries to return. Must be between 1 and 100.",
        ),
    ] = 5


class GetTools(BaseModel):
    table_name: str = TABLE_NAME
