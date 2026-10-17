"""Pydantic response contracts produced by the pipeline's LLM agents."""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field


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
    id: str = Field(description="The id of the evidence item (for example 'E1').")
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
    result_ids: list[str] = Field(
        description=(
            "The captured tool-result ids (for example 'R1') from the "
            "captured-results catalog that back this evidence item. Cite only "
            "ids that appear in that catalog. May be empty when the item "
            "collected nothing, for example an unavailable item."
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
    status: Literal["supported", "unsupported", "inconclusive"] = Field(
        description=(
            "The conclusion based only on collected evidence. Use 'inconclusive' "
            "when evidence is missing, insufficient, ambiguous, or conflicting."
        )
    )
    evidence_ids: list[str] = Field(
        description=(
            "The ids of the evidence items used to evaluate the hypothesis. Each "
            "id must refer to an entry in the report's evidence list."
        )
    )
    contradicting_evidence_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Ids of evidence items that speak AGAINST the hypothesis (for example "
            "a counterexample query that found the error without the suspected "
            "trigger). Empty when nothing contradicts it."
        ),
    )
    next_check: str | None = Field(
        default=None,
        description=(
            "The single next diagnostic check that would confirm or refute the "
            "hypothesis when the collected logs cannot."
        ),
    )


class DataScope(BaseModel):
    """What data the evidence covers, built in code from the tool results.

    Never produced by a model: ``data_scope.build_data_scope`` derives it from
    the captured records so the answer can state its scope and filters exactly.
    """

    datasets: list[str] = Field(default_factory=list)
    time_windows: list[dict[str, str]] = Field(default_factory=list)
    filters: dict[str, list[Any]] = Field(default_factory=dict)
    measured_result_ids: list[str] = Field(default_factory=list)
    example_result_ids: list[str] = Field(default_factory=list)
    truncated_result_ids: list[str] = Field(default_factory=list)
    unknown_truncation_result_ids: list[str] = Field(default_factory=list)


class EvidenceReport(BaseModel):
    summary: str = Field(
        description=(
            "A concise summary of the investigation grounded only in the collected "
            "evidence. No fabricated or inferred results."
        )
    )
    evidence: list[Evidence] = Field(
        description=(
            "Every collected evidence item, including its collection attempts. "
            "Always provided, even for a direct data question."
        )
    )
    hypotheses: list[Hypothesis] = Field(
        default_factory=list,
        max_length=3,
        description=(
            "The investigated hypotheses and the evidence-based conclusion for each. "
            "Must be non-duplicative, plausible, and testable. Maximum of 3 hypotheses. "
            "Available only when the request required investigation; empty for a direct "
            "data question."
        ),
    )
    data_scope: DataScope | None = Field(
        default=None,
        description="Code-built data scope of the captured results; never model-written.",
    )

    coverage: str = Field(
        default="",
        description=(
            "What fraction of the data was actually examined, and at what "
            "depth. Required whenever a tool reported coverage: a bounded "
            "sweep produces false negatives by construction, so an answer "
            "that omits this invites the reader to assume the whole corpus "
            "was searched."
        ),
    )


class AnswerText(BaseModel):
    """A prose section of the final answer, rendered as markdown."""

    type: Literal["text"] = "text"
    text: str = Field(
        description=(
            "One section of the answer written as markdown prose. Never include "
            "table syntax, pipe characters, or result markers here."
        )
    )


class AnswerTable(BaseModel):
    """A tool result to display as a table, referenced by its catalog id."""

    type: Literal["table"] = "table"
    result_id: str = Field(
        description=(
            "The id of the tool result to render as a table (for example 'R1'). "
            "Must be an id from the tool result catalog."
        )
    )


AnswerBlock = Annotated[AnswerText | AnswerTable, Field(discriminator="type")]


class Finding(BaseModel):
    """One atomic claim in the answer, labelled by how it is supported."""

    id: str = Field(description="Finding id, for example 'F1'.")
    kind: Literal["measured", "hypothesis", "limitation"] = Field(
        description=(
            "'measured': directly shown by cited tool results. 'hypothesis': a "
            "possible explanation the results do not confirm. 'limitation': what "
            "the data does not establish."
        )
    )
    statement: str = Field(description="The claim in one sentence.")
    result_ids: list[str] = Field(
        default_factory=list,
        description="Tool-result ids (R#) backing the claim; required for 'measured'.",
    )


class Recommendation(BaseModel):
    """One next step, justified by findings."""

    action: str = Field(description="What to check or fix, concretely.")
    why: str = Field(description="Why this comes first, based on the findings.")
    finding_ids: list[str] = Field(
        default_factory=list, description="Ids of the findings that support it."
    )
    owner_team: str | None = Field(
        default=None, description="Owning team from the ownership mapping, if known."
    )


class AnswerOutput(BaseModel):
    """The final answer: role narrative blocks plus a labelled evidence ledger.

    ``blocks`` carry the role-structured narrative and tables. ``findings`` and
    ``recommendations`` are rendered by code (``pipeline/answers.py``) into
    labelled sections, followed by a code-built "Data scope and queries"
    appendix, so measured facts, hypotheses and limitations are always
    separated and every number stays checkable.
    """

    blocks: list[AnswerBlock] = Field(
        description=(
            "The answer in reading order. Mix prose blocks with table blocks so each "
            "referenced table appears where it belongs. Show only the tables the "
            "reader needs and reference only ids from the tool result catalog."
        )
    )
    findings: list[Finding] = Field(
        default_factory=list,
        description="Atomic claims, each labelled measured, hypothesis or limitation.",
    )
    recommendations: list[Recommendation] = Field(
        default_factory=list,
        max_length=5,
        description="Ordered next steps, each justified by findings.",
    )
    follow_up: str | None = Field(
        default=None,
        description=(
            "At most one short, natural follow-up question to ask the user when the "
            "next turn would materially improve with more information. Use null when "
            "no follow-up is warranted; never restate the answer or pad with questions."
        ),
    )
