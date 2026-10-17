"""State-dependent prompts and atomic TypeSafe questions for the pipeline.

The TypeSafe questions are independent judgments evaluated in one request. Code
in ``decision.py`` owns routing and revision policy; Jev supplies only the
narrow semantic signals that code cannot derive reliably from text.
"""

from langchain_typesafe import ClassifierRequest, Noul, NoulCriteria

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
#
# Each entry names the TIER it reads, because cost differs by orders of
# magnitude between them and the agent is expected to spend cheaply first:
#
#   index   -- a local inverted index. Fast, exact, no object-storage traffic.
#   remote  -- Parquet in object storage, read in place. Costs network and time
#              in proportion to the time window, and aggregates are approximate.
TOOL_CATALOG = {
    "get_trend_tool": (
        "TIER index|remote. Counts one known service/entity/event per time "
        "bucket; params: dataset, start_time, end_time, type=(field, exact "
        "value), bucket (second|minute|hour|day), category (severity). "
        "Use to see how one known entity's volume moves over time."
    ),
    "count_by_category_tool": (
        "TIER index|remote. CHEAPEST aggregate: reads only the timestamp, "
        "severity and grouping columns, never message text; params: dataset, "
        "start_time, end_time, type (general|service|entity_id|event_id), "
        "category, limit. Use this FIRST to find where the problem is before "
        "paying to search text."
    ),
    "sweep_chunks_tool": (
        "TIER sweep. LAST RESORT, and only when the user insists on evidence "
        "from the WHOLE corpus and a bounded query will not do; params: "
        "dataset, question, max_promotions (0-4), start_at, confirm_cost. "
        "Examines all 1,624 chunks in graded passes -- footers, then aggregates "
        "that read no message text, then stratified samples -- and ranks them. "
        "max_promotions>0 reads message text for that many top chunks at about "
        "ELEVEN MINUTES EACH. ALWAYS call with confirm_cost=false first, show "
        "the user the estimate, then call again with confirm_cost=true. Report "
        "the returned coverage: a profiled chunk was counted, not read. The "
        "result also carries a `trace` of every step and the score behind each "
        "decision -- cite it when explaining why a particular chunk was read. "
        "`sampling` gives each sample's examined fraction and a likely RANGE "
        "for its match rate: quote the range rather than the single number, "
        "because a sample of ~0.3% of a chunk in a few windows is a cheap "
        "guess, not a measurement."
    ),
    "investigate_chunk_tool": (
        "TIER sweep. Reads one named chunk's message text and returns counts, "
        "templates and verbatim lines; params: dataset, chunk_id, question. "
        "Use on a chunk a sweep ranked highly when you need quotable evidence. "
        "Costs about eleven minutes. Reading one chunk says nothing about the "
        "rest of the corpus, and the returned coverage says so."
    ),
    "search_logs_tool": (
        "TIER index ONLY. Searches message text and ranks by relevance; "
        "params: dataset, query_text, category, limit. Requires an indexed "
        "dataset -- it is refused on the raw remote corpus, which has no "
        "index. Use to read the actual log lines once a window is narrowed."
    ),
    "top_in_category_by_service_tool": (
        "TIER index|remote. Ranks a field's values by log volume in a window; "
        "params: dataset, start_time, end_time, field, category, limit. "
        "Use to locate the noisiest services."
    ),
    "compare_periods_tool": (
        "TIER index|remote. Compares counts between a baseline and a recent "
        "window; params: dataset, baseline_start/end, recent_start/end, field, "
        "limit. Scans BOTH windows, so it costs roughly twice a single count. "
        "Use to detect what changed."
    ),
    "get_data_tool": (
        "TIER index|remote. Lists a field's most common values with counts; "
        "params: dataset, start_time, end_time, type, limit. Ranked by volume, "
        "so the first rows are the significant ones. Use to discover a valid "
        "filter value before requesting a trend. Refused for fields whose "
        "distinct set is effectively unbounded, such as trace ids."
    ),
    "get_log_by_line_id_tool": (
        "TIER index. Fetches one complete row by primary key; params: dataset, "
        "line_id. Only datasets that declare a record id support it -- the raw "
        "remote corpus has no primary key and refuses this."
    ),
}

# How the agent is expected to spend its budget. Code enforces the bounds; this
# tells the model why they exist so it plans within them instead of hitting them.
COST_DISCIPLINE = (
    "Datasets differ in size by six orders of magnitude, so cost discipline is "
    "part of the task.\n"
    "1. Every query that scans rows REQUIRES a time window, and the window is "
    "the main cost control. On the remote corpus one second is already about "
    "560,000 rows.\n"
    "2. Work cheap to expensive: counts and trends (which read two or three "
    "columns) before text search (which reads message bodies).\n"
    "3. Narrow before searching. Use a count to find the interesting minute, "
    "then search inside it.\n"
    "4. Text search needs an indexed dataset. On the remote corpus, aggregate "
    "first to find the window, then search the indexed slice.\n"
    "5. A refusal is a useful answer, not a failure. It names what was wrong "
    "and suggests a narrower request -- follow the suggestion and retry rather "
    "than abandoning the line of enquiry.\n"
    "6. Results from a dataset that does not promise exact counts are "
    "APPROXIMATE and must be described that way in the answer. Never present "
    "an approximate aggregate as an exact figure.\n"
    "7. Each tool result carries query_stats describing what it actually read. "
    "Use those numbers when stating how much data the investigation covered."
)

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


# Short, code-owned vocabulary used by the scope judgment. The database schema
# is intentionally not sent: the scope gate only needs to know what counts as a
# queryable target, not how every table is implemented.
#
# ``dataset`` is included because the corpus is now part of what makes a request
# answerable -- a question about the OTel demo app cannot be served from the
# OpenStack sample, and a request with no resolvable time window cannot be
# served from the remote corpus at all.
QUERYABLE_TARGETS = [
    "dataset or corpus (which system's logs)",
    "service",
    "entity id (process id, trace id, or node)",
    "event id",
    "event template or error/message text",
    "severity",
    "time window",
]


def dataset_catalog() -> dict:
    """What corpora exist, how big they are, and what each supports.

    Sent to the scope gate so it can judge whether a request names a corpus this
    service can actually serve. Deliberately carries no table, index, or column
    names -- the model selects a dataset *id*, and the registry resolves it.
    """
    from backend.db.datasets import catalog

    return catalog()


def summarized_attempts(tool_results: object) -> list[dict]:
    """Compact summaries of the tool calls already attempted this turn."""
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
    """Whether a normalized result is real (not missing or the empty placeholder).

    An empty/placeholder ``NormalizedOutput`` has empty ``normalized_question``
    and ``intent``; that shape must not be treated as accumulated context.
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
        # Use raw messages, not the normalizer's rewrite: target wording must
        # remain visible to the scope judgments.
        return {
            "state": {
                "latest_message": state["latest_message"],
                "transcript": state.get("messages", []),
                "latest_answer": state["latest_answer"],
                "mission": PIPELINE_MISSION,
                "queryable_targets": QUERYABLE_TARGETS,
                "datasets": dataset_catalog(),
                "tool_catalog": TOOL_CATALOG,
            },
            "questions": {
                "is_log_request": self.is_log_request_question(),
                "has_queryable_scope": self.has_queryable_scope_question(),
                "proceed_without_clarification": (
                    self.proceed_without_clarification_question()
                ),
                "needs_new_evidence": self.needs_new_evidence_question(),
            },
        }

    def is_log_request_question(self) -> Noul:
        return Noul(
            instructions={
                "question": "Is `latest_message` a request about the user's logs that this service can answer or investigate?",
                "inspect": ["`latest_message`", "`transcript`"],
                "focus": "Use `transcript` only to interpret a short follow-up.",
            },
            criteria=NoulCriteria(
                true={
                    "what": "The latest message asks about the user's logs or requests log investigation or analysis.",
                },
                false={
                    "what": "The latest message is not a log-data request.",
                    "examples": ["greeting", "small talk", "capability question", "unrelated technical or general-knowledge question"],
                },
            ),
        )

    def has_queryable_scope_question(self) -> Noul:
        return Noul(
            instructions={
                "question": "Does `latest_message`, interpreted with `transcript`, name at least one queryable target?",
                "compare": ["`latest_message`", "`transcript`", "`queryable_targets`"],
                "focus": "A target from an earlier message counts; an unnamed filter does not.",
            },
            criteria=NoulCriteria(
                true={
                    "what": "At least one target from `queryable_targets` is named, so the request can be investigated or answered now.",
                },
                false={
                    "what": "No queryable target is named anywhere in the conversation.",
                    "examples": ["what recurring problems exist?", "what is wrong in general?"],
                },
            ),
        )

    def proceed_without_clarification_question(self) -> Noul:
        return Noul(
            instructions={
                "question": "Does `latest_message` authorize proceeding without the detail requested in `latest_answer`?",
                "compare": ["`latest_message`", "`latest_answer`"],
                "focus": "Judge authorization, not how well-specified the request is.",
            },
            criteria=NoulCriteria(
                true={
                    "what": "The user declines to provide the requested detail or explicitly authorizes the assistant to choose and proceed.",
                    "examples": ["just do it", "pick one", "use whichever", "use your judgement", "I don't care, proceed", "no more questions"],
                },
                false={
                    "what": "The user supplies the detail, restates the task, or does not address the clarification.",
                    "not_for": ["I want you to do it", "plain task intent without authorization to choose missing parameters"],
                },
            ),
        )

    def needs_new_evidence_question(self) -> Noul:
        return Noul(
            instructions={
                "question": "Does answering `latest_message` require log records not already present in `latest_answer` or `transcript`?",
                "compare": ["`latest_message`", "`latest_answer`", "`transcript`", "`tool_catalog`"],
                "focus": "Judge whether retrieval is needed, not whether the request is generally about logs.",
            },
            criteria=NoulCriteria(
                true={
                    "what": "A search, filter, count, aggregation, comparison, or verification of log records is needed.",
                },
                false={
                    "what": "The request can be answered from existing information.",
                    "examples": ["explanation", "summary", "interpretation", "next step based on existing evidence"],
                },
            ),
        )

    def confidence_request(self, state: PipelineState) -> ClassifierRequest:
        return {
            "state": {
                "request": state["normalized"],
                "role": state["role"],
                "evidence_report": state.get("evidence_report", {}),
                "answer": state.get("answer", ""),
                "tool_catalog": TOOL_CATALOG,
                "attempted_tools": summarized_attempts(state.get("tool_results")),
            },
            "questions": {
                "evidence_relevant": self.evidence_relevant_question(),
                "evidence_gap_remains": self.evidence_gap_remains_question(),
                "gap_closable_by_untried_tool": self.gap_closable_by_untried_tool_question(),
                "answer_grounded": self.answer_grounded_question(),
                "answer_fits_role": self.answer_fits_role_question(),
                "answer_complete": self.answer_complete_question(),
                "worth_follow_up": self.worth_follow_up_question(),
            },
        }

    def evidence_relevant_question(self) -> Noul:
        return Noul(
            instructions={
                "question": "Does `evidence_report` address the user's `request`?",
                "compare": ["`evidence_report`", "`request`"],
                "focus": "Judge relevance to the request, not whether the report is polished.",
            },
            criteria=NoulCriteria(
                true={"what": "The report contains evidence that directly addresses the requested question."},
                false={"what": "The report is missing the requested subject, dimensions, or comparison."},
            ),
        )

    def evidence_gap_remains_question(self) -> Noul:
        return Noul(
            instructions={
                "question": "Does a specific missing database fact remain that would materially change `answer` for `request`?",
                "compare": ["`request`", "`evidence_report`", "`answer`"],
                "focus": "Judge whether new log retrieval is needed. Do not treat a desire to rewrite, add detail, or improve role fit as a database evidence gap.",
            },
            criteria=NoulCriteria(
                true={"what": "A specific missing fact or check could materially change the answer."},
                false={"what": "The report contains the facts needed to answer `request`, even if `answer` needs rewriting for grounding, role fit, or completeness."},
            ),
        )

    def gap_closable_by_untried_tool_question(self) -> Noul:
        return Noul(
            instructions={
                "question": "Assuming a material evidence gap remains, is there a concrete untried query in `tool_catalog` that could plausibly close it?",
                "compare": ["`request`", "`evidence_report`", "`attempted_tools`", "`tool_catalog`"],
                "focus": "This is a speculative availability check; ignore it when no gap remains.",
            },
            criteria=NoulCriteria(
                true={"what": "An available tool has a specific query not already attempted that could address the gap."},
                false={"what": "No available untried tool can address the gap, or the needed data is unavailable."},
            ),
        )

    def answer_grounded_question(self) -> Noul:
        return Noul(
            instructions={
                "question": "Are the material claims in `answer` supported by `evidence_report`?",
                "compare": ["`answer`", "`evidence_report`"],
                "focus": "Check claims, numbers, causes, and evidence status; do not reward fluent unsupported prose.",
            },
            criteria=NoulCriteria(
                true={"what": "Material claims are supported by the report and do not invent facts."},
                false={"what": "A material claim is unsupported, contradicted, or presented with invented certainty."},
            ),
        )

    def answer_fits_role_question(self) -> Noul:
        return Noul(
            instructions={
                "question": "Does `answer` serve the user's `role.objective` at an appropriate level?",
                "compare": ["`answer`", "`role.objective`"],
                "focus": "Judge the answer's decision usefulness for the stated role, not whether it is generally readable.",
            },
            criteria=NoulCriteria(
                true={"what": "The answer supports the role objective with appropriate emphasis and detail."},
                false={"what": "The answer substantially serves a different role or omits what this role needs."},
            ),
        )

    def answer_complete_question(self) -> Noul:
        return Noul(
            instructions={
                "question": "Does `answer` resolve `request` or clearly state its evidence-based limitations?",
                "compare": ["`answer`", "`request`", "`evidence_report`"],
                "focus": "Completeness means addressing the question, not including every possible detail.",
            },
            criteria=NoulCriteria(
                true={"what": "The answer resolves the request or explicitly explains the remaining limitation."},
                false={"what": "Important parts of the request are unanswered and the limitation is not stated."},
            ),
        )

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
            f"Scan budget: at most ~{spec.max_rows_scanned:,} rows read across "
            "the whole investigation. Each tool result reports what it actually "
            "read in query_stats.\n"
            "</effort guidance>\n"
            "<cost discipline>\n"
            f"{COST_DISCIPLINE}\n"
            "</cost discipline>\n"
            "<datasets>\n"
            f"{dataset_catalog()}\n"
            "</datasets>\n"
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
