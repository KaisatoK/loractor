"""State-dependent prompt builders used by the pipeline nodes.

``PipelinePrompts`` renders the per-stage prompts from ``PipelineState``. The
TypeSafe classifier question builders (``needs_clarification_question``,
``proceed_without_clarification_question``, ``route_question``, ``pass_question``,
``next_state_question``, ``revision_reason_question``,
``worth_follow_up_question``) back the scope and confidence routers wired up in
``graph.py``.
"""

from langchain_typesafe import Choice, ClassifierRequest, Noul, NoulCriteria

from backend.db.config import LOG_SCHEMA
from backend.pipeline.effort import get_effort_spec
from backend.pipeline.state import PipelineState, Role

ANSWER_EFFORT_NOTES = {
    "Low": "Be brief: lead with the verdict, keep it under ~150 words, at most one table, skip caveats unless critical.",
    "Medium": "Balanced: key findings with brief evidence references, at most two tables, short next steps.",
    "High": "Thorough: full role structure, evidence references, a timeline if relevant, explicit limitations.",
    "Extreme": "Exhaustive: every material finding, per-hypothesis status, limitations, and confidence per claim.",
    "Brutal": "Maximum depth: everything from Extreme plus alternative explanations considered and ruled out, and explicit uncertainty bounds.",
}

# Authoritative catalog of the evidence tools the pipeline can actually call.
# Its key set MUST equal ``backend.pipeline.tools.__all__`` (asserted by
# tests/test_confidence_prompts.py). The scope and confidence classifiers see it
# so they reason about what evidence is still retrievable before choosing a
# route -- in particular whether another find_evidence round could help at all.
TOOL_CATALOG = {
    "get_trend_tool": (
        "Counts one known pid/service/event_id BY MINUTE over a time window; "
        "params: start_time, end_time, type=(kind, exact value), category (severity). "
        "Use to see how one known entity's volume moves over time."
    ),
    "count_by_category_tool": (
        "Counts log rows grouped by severity/type; "
        "params: start_time, end_time, type (pid|service|event_id|general), category (severity). "
        "Use to quantify volume overall or per pid/service/event_id."
    ),
    "search_logs_tool": (
        "Searches message text (optionally filtered by severity); "
        "params: query_text, category (severity), limit. "
        "Use to find and read the actual log lines."
    ),
    "top_in_category_by_service_tool": (
        "Ranks services by log volume within a window, optionally one severity; "
        "params: start_time, end_time, category (severity), limit. "
        "Use to locate the noisiest services."
    ),
    "compare_periods_tool": (
        "Compares service/severity counts between a baseline and a recent window; "
        "params: baseline_start, baseline_end, recent_start, recent_end, limit. "
        "Use to detect what changed between two time ranges."
    ),
    "get_data_tool": (
        "Lists the known pids/services/event_ids in the log; "
        "params: type (pid|service|event_id). "
        "Use to discover valid filter values before querying."
    ),
    "get_log_by_line_id_tool": (
        "Fetches one complete log row by its line_id (primary key); "
        "params: line_id. "
        "Use to resolve a 'Line ID: n' reference into the full log record."
    ),
}

# Mission of the pipeline, given to the scope classifier as context so it can
# judge whether a request is underspecified *relative to what this service does*.
PIPELINE_MISSION = (
    "This service answers a user's question about their logs. It investigates a "
    "specific failure for an engineer, finds recurring problems and patterns for a "
    "team lead, and prioritizes issues by impact for a manager. It turns each request "
    "into evidence gathered from the connected log database (counts, searches, trends, "
    "and period comparisons), and then into a grounded answer with a recommendation. "
    "It cannot reach any system outside that log database."
)


def summarized_attempts(tool_results: object) -> list[dict]:
    """Compact summaries of the tool calls already attempted this turn.

    Derived from ``state["tool_results"]`` records
    (``{id, tool_name, arguments, row_count, rows}``) so the confidence
    classifier can tell which queries have already run -- and avoid routing back
    to find_evidence for a query that would only duplicate existing results.
    """
    summary = []
    for record in tool_results or []:
        summary.append(
            {
                "tool_name": record.get("tool_name"),
                "arguments": record.get("arguments"),
                "row_count": record.get("row_count"),
            }
        )
    return summary


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
        "Do not invent facts, causes, impact, or missing details. When the report "
        "contains hypotheses, treat supported hypotheses as findings, contradicted "
        "hypotheses as excluded, and unresolved hypotheses as limitations; when it "
        "does not, ground the answer directly in the report's summary and evidence "
        "instead. Include relevant time ranges, filters, calculations, evidence "
        "references, and confidence."
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
                "latest_message": state["latest_message"],
                "latest_answer": state["latest_answer"],
                "database_schema": LOG_SCHEMA,
                "tool_catalog": TOOL_CATALOG,
                "mission": PIPELINE_MISSION,
            },
            "questions": {
                "needs_clarification": self.needs_clarification_question(),
                "proceed_without_clarification": (
                    self.proceed_without_clarification_question()
                ),
                "route": self.route_question(),
            },
        }

    def needs_clarification_question(self) -> Noul:
        return Noul(
            instructions=(
                "Decide whether producing a useful response or beginning a meaningful "
                "investigation requires more information from the user. "
                "Use the full conversation to interpret follow-up questions and "
                "references to earlier messages."
            ),
            criteria=NoulCriteria(
                true=(
                    "The request is too ambiguous or underspecified to answer or "
                    "investigate now; ask the user to clarify."
                ),
                false=(
                    "The request is specific enough to answer or investigate now "
                    "without asking the user for more information."
                ),
            ),
        )

    def proceed_without_clarification_question(self) -> Noul:
        return Noul(
            instructions=(
                "Decide whether the user has authorized proceeding without the "
                "detail the previous assistant message asked for. Judge the latest "
                "user message in relation to that previous assistant message. "
                "This is about authorization, not about how well-specified the "
                "request is: a vague request can still be authorized. Restating "
                "the task in other words is not authorization to choose missing "
                "parameters."
            ),
            criteria=NoulCriteria(
                true=(
                    "The user explicitly declined to supply the requested detail, "
                    "or instructed the assistant to proceed without it, for example "
                    "'just do it', 'pick one', 'use whichever', 'use your "
                    "judgement', 'I don't care, proceed', or 'no more questions'."
                ),
                false=(
                    "The user supplied the requested detail, restated the task "
                    "without declining to provide it, did not address the question, "
                    "or expressed plain task intent such as 'I want you to do it', "
                    "which is not authorization to choose missing parameters."
                ),
            ),
        )

    def route_question(self) -> Choice:
        return Choice(
            instructions=(
                "Assuming the request can be answered or investigated without asking "
                "the user for more information, choose the action that answers it. "
                "Use the full conversation to interpret follow-up questions and "
                "references to earlier messages."
            ),
            criteria={
                "general": (
                    "Respond directly as a general assistant. The request is unrelated "
                    "to log investigation: greetings, small talk, questions about what "
                    "the assistant can do, or unrelated technical questions."
                ),
                "direct_answer": (
                    "Answer without retrieving new log evidence. The request concerns "
                    "log investigation but the needed evidence is already available in "
                    "the conversation, or it asks for an explanation, summary, "
                    "interpretation, or next step based on existing information."
                ),
                "find_evidence": (
                    "Retrieve new log evidence before answering. The request concerns "
                    "log investigation and cannot be answered accurately without "
                    "searching, filtering, counting, aggregating, correlating, "
                    "comparing, or verifying log records not already provided."
                ),
            },
        )

    def confidence_request(self, state: PipelineState) -> ClassifierRequest:
        revision_count = state.get("revision_count", 0)
        return {
            "state": {
                "request": state["normalized"],
                "role": state["role"],
                "evidence_report": state.get("evidence_report", {}),
                "answer": state.get("answer", ""),
                "revision_feedback": state.get("revision_feedback", ""),
                "tool_catalog": TOOL_CATALOG,
                "evidence_report_history": state.get("evidence_report_history", []),
                "attempted_tools": summarized_attempts(state.get("tool_results")),
                "revision_count": revision_count,
            },
            "questions": {
                "pass": self.pass_question(state["role"]),
                "next_state": self.next_state_question(state["role"]),
                "revision_reason": self.revision_reason_question(),
                "worth_follow_up": self.worth_follow_up_question(),
            },
        }

    def worth_follow_up_question(self) -> Noul:
        return Noul(
            instructions=(
                "Decide whether asking the user a single, specific follow-up question "
                "would materially improve the next turn's answer or investigation. "
                "Say no when the answer is complete or a question would be awkward, "
                "redundant, or only marginally useful."
            ),
            criteria=NoulCriteria(
                true=(
                    "A specific, natural follow-up question would materially improve "
                    "the next turn, for example it would unlock missing evidence or a "
                    "clearer scope."
                ),
                false=(
                    "The answer is complete, or no single follow-up question would "
                    "meaningfully improve the next turn."
                ),
            ),
        )

    def clarify(self, state: PipelineState) -> str:
        return (
            "<role context>\n"
            f"{self.role_context(state['role'])}\n"
            "</role context>\n"
            "<request>\n"
            f"{state['normalized']}\n"
            "</request>\n"
            "<conversation>\n"
            f"{state.get('conversation', '')}\n"
            "</conversation>\n\n"
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
            f"Tool-call budget: at most {spec.max_tool_calls} tool calls in this investigation (hard limit; to-do updates count).\n"
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
            f"{state.get('evidence_report', {})}\n"
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
                    "grounding": "Material claims, evidence statuses, and any hypotheses are supported by tool results.",
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
                "Choose the next pipeline state based on evidence sufficiency, "
                "answer quality, and the availability of a useful next action.\n\n"
                f"{self.role_context(role)}"
            ),
            criteria={
                "find_evidence": (
                    "A specific evidence gap remains and there is "
                    "at least one concrete, untried, tool-executable query that could "
                    "plausibly close that gap and materially change the answer. The "
                    "gap must be addressable by an available catalog tool. Do not "
                    "choose this state for a previously attempted query, speculative "
                    "research, unavailable data, or evidence that would not affect "
                    "the answer."
                ),
                "answer": (
                    "The available evidence is sufficient, but the answer "
                    "still needs to be written or materially revised. This includes "
                    "improving role fit, reasoning, organization, clarity, or the use "
                    "of evidence already collected. No additional evidence retrieval "
                    "is needed."
                ),
                "end": (
                    "The answer is grounded, role-appropriate, and ready "
                    "to return. Also when a remaining deficiency "
                    "cannot be resolved with any concrete, untried query supported by "
                    "the available tools."
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
                "irrelevant_evidence": "The evidence does not address the request.",
                "unsupported_answer": "The evidence is adequate, but the answer misuses or omits it.",
                "role_mismatch": "The answer does not support the user's role objective.",
                "none": "The result is grounded and ready to return.",
            },
        )
