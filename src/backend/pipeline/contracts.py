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


class AnswerOutput(BaseModel):
    """The final answer as an ordered sequence of prose and table blocks."""

    blocks: list[AnswerBlock] = Field(
        description=(
            "The answer in reading order. Mix prose blocks with table blocks so each "
            "referenced table appears where it belongs. Show only the tables the "
            "reader needs and reference only ids from the tool result catalog."
        )
    )
    follow_up: str | None = Field(
        default=None,
        description=(
            "At most one short, natural follow-up question to ask the user when the "
            "next turn would materially improve with more information. Use null when "
            "no follow-up is warranted; never restate the answer or pad with questions."
        ),
    )
