from typing import Annotated, Any, Literal, TypedDict

from pydantic import BaseModel, Field

from backend.pipeline.tools.utils import TABLE_NAME

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
        dict[str, Any], Field(description="Dynamic question-specific JSON.")
    ]
    ambiguities: Annotated[
        list[str], Field(description="Uncertainties that could affect the result.")
    ]


class EvidenceHypothesis(BaseModel):
    hypothesis: Annotated[
        str,
        Field(
            description="A plausible explanation or investigative direction for the problem."
        ),
    ]
    description: Annotated[
        str, Field(description="Why this hypothesis could explain the user's problem.")
    ]
    required_evidence: Annotated[
        list[str],
        Field(description="Facts that must be observed to evaluate this hypothesis."),
    ]


class ExecutionPlan(BaseModel):
    hypotheses: Annotated[
        list[EvidenceHypothesis],
        Field(
            description="Investigation hypotheses and the evidence required to evaluate them."
        ),
    ]


class EvidenceStatus(BaseModel):
    requirement: Annotated[
        str, Field(description="The required evidence being evaluated.")
    ]
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
    effort: Literal["Low", "Medium", "High", "Extreme", "Brutal"]
    scope: Literal["pipeline", "general"]
    normalized: NormalizedOutput
    plan: ExecutionPlan
    plan_history: list[PlanAttempt]
    evidence: dict[str, Any]
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
            description="Text or concept to search for in log messages. Do not include INFO, WARNING, ERROR (or any case variations like info, Warning, Err) merely as severity filters.",
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
