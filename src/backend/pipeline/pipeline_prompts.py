from langchain_typesafe import Choice, ClassifierRequest, Noul, NoulCriteria

from backend.pipeline.effort import get_effort_spec
from backend.pipeline.schema.schemas import PipelineState, Role
from backend.pipeline.utils import LOG_SCHEMA

ANSWER_EFFORT_NOTES = {
    "Low": "Be brief: lead with the verdict, keep it under ~150 words, at most one table, skip caveats unless critical.",
    "Medium": "Balanced: key findings with brief evidence references, at most two tables, short next steps.",
    "High": "Thorough: full role structure, evidence references, a timeline if relevant, explicit limitations.",
    "Extreme": "Exhaustive: every material finding, per-hypothesis status, limitations, and confidence per claim.",
    "Brutal": "Maximum depth: everything from Extreme plus alternative explanations considered and ruled out, and explicit uncertainty bounds.",
}


def normalized_is_usable(previous: object) -> bool:
    """Whether a normalized result is real (not missing or the empty fallback).

    The error middleware falls back to ``NormalizedOutput`` with empty
    ``normalized_question`` and ``intent``; that shape must not be treated as
    accumulated context.
    """
    if isinstance(previous, dict):
        return bool(previous.get("normalized_question")) or bool(previous.get("intent"))
    return bool(getattr(previous, "normalized_question", "")) or bool(
        getattr(previous, "intent", "")
    )


class PipelinePrompts:
    """Build the prompts used by each stage of the pipeline."""

    shared_grounding = (
        "Use only observed tool results. Separate evidence from interpretation. "
        "Do not invent facts, causes, impact, or missing details. Treat supported "
        "hypotheses as findings, contradicted hypotheses as excluded, and unresolved "
        "hypotheses as limitations. Include relevant time ranges, filters, calculations, "
        "evidence references, and confidence."
    )

    REFRAME_INSTRUCTION = (
        "This is a REFRAME of an already-completed investigation for a different role. "
        "Re-explain the SAME findings for the target role's decision needs. "
        "Base every claim exclusively on the provided evidence report and tool result "
        "catalog. "
        "Do not introduce new facts, causes, numbers, or impact that the evidence does "
        "not support. "
        "The marker rules apply unchanged: reuse only ids from the tool result catalog."
    )

    def role_context(self, role: Role) -> str:
        return f"User role: {role['name']}\n" f"Role objective: {role['objective']}\n\n"

    def scope_request(self, state: PipelineState) -> ClassifierRequest:
        return {
            "state": {
                "conversation": state["normalized"],
                "database_schema": LOG_SCHEMA,
            },
            "questions": {
                "route": self.scope_question(),
            },
        }

    def scope_question(self) -> Noul:
        return Choice(
            instructions=(
                "Classify the user's latest request based on whether it concerns "
                "log investigation and whether answering it requires retrieving "
                "new evidence from the log database. "
                "Use the full conversation to interpret follow-up questions and "
                "references to earlier messages."
            ),
            criteria={
                "irrelevant": (
                    "The user's request is unrelated to log investigation. This includes "
                    "general conversation, unrelated technical questions, and requests "
                    "that do not involve examining or reasoning about logs."
                ),
                "resolved": (
                    "The user's request concerns log investigation, but it can be answered "
                    "without retrieving new log evidence. The necessary evidence is already "
                    "available in the conversation, or the request asks for an explanation, "
                    "summary, interpretation, or next step based on existing information."
                ),
                "unresolved": (
                    "The user's request concerns log investigation and cannot be answered "
                    "accurately without retrieving new evidence from the log database. This "
                    "includes requests to search, filter, count, aggregate, correlate, compare, "
                    "or verify log records that have not already been provided."
                ),
            },
        )

    def normalize(self, state: PipelineState) -> str:
        # Raw tool_results are intentionally excluded from the carried context:
        # they are display-only, regenerated every turn by find_evidence, and
        # including their rows would bloat the compact prompt.
        previous = state.get("normalized")

        if previous and normalized_is_usable(previous):
            prompt = (
                "<previous normalized context>\n"
                f"{previous}\n"
                "</previous normalized context>\n"
            )
            latest_answer = state.get("latest_answer", "")
            if latest_answer:
                prompt += (
                    "<previous answer>\n" f"{latest_answer}\n" "</previous answer>\n"
                )
            prompt += (
                "<new message>\n" f"{state['latest_message']}\n" "</new message>\n"
            )
            return prompt

        return "<conversation>\n" f"{state['conversation']}\n" "</conversation>\n\n"

    def find_evidence(self, state: PipelineState) -> str:
        previous_report = state.get("evidence_report", [])
        revision_feedback = state.get(
            "revision_feedback", "No previous attempt exists."
        )
        spec = get_effort_spec(state.get("effort"))

        return (
            "<role context>\n"
            f"{self.role_context(state['role'])}\n"
            "</role context>\n"
            "<effort guidance>\n"
            f"Effort level: {state.get('effort', 'Medium')}.\n"
            f"Tool-call budget: at most {spec.max_tool_calls} tool calls (hard limit).\n"
            f"Hypothesis budget: define at most {spec.max_hypotheses} hypotheses (never more than 3).\n"
            "</effort guidance>\n"
            "<request>\n"
            f"{state['normalized']}\n"
            "</request>\n"
            "<previous evidence report>\n"
            f"{previous_report}\n"
            "</previous evidence report>\n"
            "<revision feedback>\n"
            f"{revision_feedback}\n"
            "</revision feedback>\n"
            "<database schemas>\n"
            f"{LOG_SCHEMA}\n"
            "</database schemas>\n\n"
        )

    def answer(self, state: PipelineState) -> str:
        revision_feedback = state.get(
            "revision_feedback",
            "No previous attempt exists.",
        )

        # One result per block, newline-separated — deliberately NO pipe (`|`)
        # separators: the catalog is shown to the model, and pipe-delimited
        # lines primed it to emit stray markdown-table pipes in the answer.
        catalog_lines = []
        for record in state.get("tool_results", []) or []:
            rows = record.get("rows") or []
            columns = ", ".join(rows[0].keys()) if rows else ""
            preview = " ; ".join(
                "{" + ", ".join(f"{k}: {str(v)[:60]}" for k, v in row.items()) + "}"
                for row in rows[:2]
            )
            catalog_lines.append(
                f"- id: {record.get('id')}\n"
                f"  marker: [[RESULT:{record.get('id')}]]\n"
                f"  tool: {record.get('tool_name')}\n"
                f"  args: {record.get('arguments')}\n"
                f"  rows: {record.get('row_count')}\n"
                f"  columns: {columns}\n"
                f"  preview: {preview}"
            )
        tool_result_catalog = "\n".join(catalog_lines)

        return (
            "<grounding rules>\n"
            f"{self.shared_grounding}\n\n"
            "</grounding rules>\n"
            "<role context>\n"
            f"{self.role_context(state['role'])}\n"
            "</role context>\n"
            "<effort guidance>\n"
            f"Depth guidance: {ANSWER_EFFORT_NOTES.get(state.get('effort', 'Medium'), ANSWER_EFFORT_NOTES['Medium'])}\n"
            "</effort guidance>\n"
            "<request>\n"
            f"{state['normalized']}\n"
            "</request>\n"
            "<evidence report>\n"
            f"{state['evidence_report']}\n"
            "</evidence report>\n"
            "<tool result catalog>\n"
            f"{tool_result_catalog}\n"
            "</tool result catalog>\n"
            "<revision feedback>\n"
            f"{revision_feedback}\n"
            "</revision feedback>\n\n"
        )

    def reframe(self, state: PipelineState) -> str:
        return (
            f"{self.answer(state)}"
            "<previous answer>\n"
            f"{state.get('latest_answer', '')}\n"
            "</previous answer>\n"
            "<reframe instructions>\n"
            f"{self.REFRAME_INSTRUCTION}\n"
            "</reframe instructions>\n"
        )

    # def confidence_request(self, state: PipelineState) -> ClassifierRequest:
    #     role_guidance = self.role_guidance(state["role"])
    #     return {
    #         "state": {
    #             "user_role": state["role"]["name"],
    #             "role_objective": state["role"]["objective"],
    #             "user_question": state["question"],
    #             "hypotheses": state["plan"],
    #             "evidence_report": state["evidence"],
    #             "answer": state.get("answer", ""),
    #         },
    #         "question": {
    #             "pass": self.pass_question(role_guidance),
    #             "next_state": self.next_state_question(role_guidance),
    #             "revision_reason": self.revision_reason_question(),
    #         },
    #     }

    def pass_question(self, role: Role) -> Noul:
        return Noul(
            instructions=(
                "Estimate whether the current evidence and answer are sufficient "
                "to answer the user's question accurately and for the user's role.\n\n"
                f"{self.role_context(role)}"
            ),
            criteria=NoulCriteria(
                true={
                    "role_fit": "The answer supports the role objective at the appropriate level of detail.",
                    "grounding": "Material claims and hypothesis statuses are supported by tool results.",
                    "completeness": "The answer resolves the question or clearly states its limitations.",
                },
                false={
                    "role_fit": "The answer does not support the role objective.",
                    "grounding": "The answer contains unsupported claims or misrepresents evidence.",
                    "completeness": "Important findings or limitations are missing.",
                },
            ),
        )

    def next_state_question(self, role: Role) -> Choice:
        return Choice(
            instructions=(
                "Choose the next pipeline state based on evidence sufficiency and "
                "answer quality.\n\n"
                f"{self.role_context(role)}"
            ),
            criteria={
                "find_evidence": (
                    "Evidence is missing, unavailable, irrelevant, contradictory, "
                    "or no important hypothesis is supported."
                ),
                "answer": (
                    "Evidence is sufficient, but the answer must be generated or revised."
                ),
                "end": (
                    "The answer is grounded, role-appropriate, and ready to return."
                ),
            },
        )

    def revision_reason_question(self) -> Choice:
        return Choice(
            instructions=(
                "Identify the main reason the current attempt should be revised. "
                "Choose none only when the evidence and answer are ready to return."
            ),
            criteria={
                "missing_evidence": "More evidence is required.",
                "irrelevant_evidence": "The evidence does not address the important hypotheses.",
                "unsupported_answer": "The evidence is adequate, but the answer misuses or omits it.",
                "role_mismatch": "The answer does not support the user's role objective.",
                "none": "The result is grounded and ready to return.",
            },
        )
