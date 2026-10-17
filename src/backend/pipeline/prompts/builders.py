"""State-dependent prompts and atomic TypeSafe questions for the pipeline.

The TypeSafe questions are independent judgments evaluated in one request. Code
in ``decision.py`` owns routing and revision policy; Jev supplies only the
narrow semantic signals that code cannot derive reliably from text.
"""

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass

from langchain_typesafe import Choice, ClassifierRequest, Noul, NoulCriteria

from backend.db.config import LOG_SCHEMA
from backend.pipeline.claims import MAX_CLAIM_ROWS, ClaimTarget, relevant_rows
from backend.pipeline.effort import get_effort_spec
from backend.pipeline.playbooks import ownership_context, role_answer_rules, role_playbook
from backend.pipeline.state import PipelineState, Role
from backend.pipeline.tools.envelope import CUT_MARKER, MEASURED_KINDS
from backend.pipeline.tools.specs import pipeline_specs

logger = logging.getLogger(__name__)

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
# What the classifiers (scope and confidence) know about the evidence tools: one
# line each, read from the spec table (``tools/specs.py``) so the catalog cannot
# drift from the tools. Planned tools are listed with a "NOT AVAILABLE YET" line.
TOOL_CATALOG = {spec.name: spec.catalog for spec in pipeline_specs()}

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
    "first to find the window, then search otel_slice with that window -- an "
    "index built in place over the same files, so it copies nothing but takes "
    "about a second per search because each match is re-read from object "
    "storage. The window you search is indexed automatically if the current "
    "index does not cover it, which takes MINUTES, so narrow the window with "
    "aggregates before searching rather than indexing a wide one. Never "
    "attempt full-text search on otel_s3.\n"
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
    "event template or error/message text",
    "severity",
    "time window",
]


PREVIEW_CHARS = 240


def _clip(value: object, limit: int = PREVIEW_CHARS) -> str:
    """A cell value for the result catalog, with an explicit marker when cut.

    A silent cut made the answer quote a shortened hash as if it were the real
    value; the ``…[cut]`` marker tells the model the text is incomplete. The
    answer's own tables always show the full stored row.
    """
    text = str(value)
    return text if len(text) <= limit else text[:limit] + CUT_MARKER


def _claim_result(record: dict, statement: str) -> dict:
    """A cited result as the claim check sees it: scope, totals and relevant rows."""
    rows = record.get("rows") or []
    shown = relevant_rows(statement, rows, MAX_CLAIM_ROWS)
    filters = {
        key: value for key, value in (record.get("filters") or {}).items() if value is not None
    }
    return {
        "id": record.get("id"),
        "tool": record.get("tool_name"),
        "kind": record.get("kind") or "unknown",
        "dataset": record.get("dataset") or "unknown",
        "filters": filters,
        "row_count": record.get("row_count", len(rows)),
        "total_matches": record.get("total_matches"),
        "rows_shown": len(shown),
        "rows": [
            {
                k: v if v is None or isinstance(v, (bool, int, float)) else _clip(v)
                for k, v in row.items()
            }
            for row in shown
        ],
    }


# How much of each tool result the answer model sees. Measured results (counts,
# trends, the derived ranking) are what the answer ranks and compares, so they
# are shown in full up to the largest tool cap; examples only need a sample.
# Starting values: confirm with the prompt sizes logged by ``answer_node`` and
# ``generate_answer`` rather than treating them as measured.
ANSWER_MEASURED_ROWS = 100
ANSWER_EXAMPLE_ROWS = 10
ANSWER_CONTEXT_ROWS = 40
ANSWER_CATALOG_CHAR_BUDGET = 120_000  # about 30k tokens at 4 chars per token
_MIN_EXAMPLE_ROWS = 1
_ANSWER_MEASURED_KINDS = MEASURED_KINDS | {"derived"}


@dataclass(frozen=True)
class ResultCatalog:
    """The answer prompt's tool-result catalog and what could not be shown."""

    text: str
    # Human-readable notes (e.g. measured rows alone exceed the budget); the
    # answer node surfaces them in the data-scope appendix.
    notes: tuple[str, ...] = ()


def _nearest_context_rows(rows: list[dict], limit: int) -> tuple[list[dict], str | None]:
    """Select context examples closest to the anchor, then restore row order.

    Nearby-event results are ordered by ``record_id`` and carry an ``offset``
    from the anchor. Taking the first rows shows the farthest preceding events
    when ``before`` is larger than the answer sample. Select by distance instead;
    a preceding row wins an equal-distance tie, the anchor is always retained
    when present, and the selected rows are put back in record-id order for the
    timeline. Results without usable offsets retain ordinary example behavior.
    """
    candidates: list[tuple[int, int, int, dict]] = []
    for position, row in enumerate(rows):
        try:
            offset = int(row["offset"])
        except (KeyError, TypeError, ValueError):
            return rows[:limit], None
        candidates.append((abs(offset), 0 if offset < 0 else 1, position, row))

    selected = [item[3] for item in sorted(candidates, key=lambda item: item[:3])[:limit]]
    selected.sort(
        key=lambda row: (
            row.get("record_id") is None,
            row.get("record_id", row.get("offset", 0)),
        )
    )
    offsets = [int(row["offset"]) for row in selected]
    if not offsets:
        return selected, None
    return selected, f"nearest the anchor, offsets {min(offsets)}..{max(offsets)}"


def _catalog_entry(record: dict, example_rows: int) -> str:
    """One result block. ``example_rows`` applies to non-measured kinds only.

    One ``{col: value}`` per row and no ``|``: pipe-delimited lines once primed
    the model to emit stray markdown-table pipes in the answer.
    """
    rows = record.get("rows") or []
    measured = record.get("kind") in _ANSWER_MEASURED_KINDS
    limit = ANSWER_MEASURED_ROWS if measured else example_rows
    context_note = None
    if record.get("kind") == "context" and not measured:
        shown, context_note = _nearest_context_rows(rows, limit)
    else:
        shown = rows[:limit]
    columns = ", ".join(rows[0].keys()) if rows else ""
    truncated = record.get("truncated")
    returned = record.get("row_count", len(rows))
    shown_label = f"{len(shown)}"
    if context_note:
        shown_label += f" ({context_note})"
    entry = (
        f"- id: {record.get('id')}\n"
        f"  tool: {record.get('tool_name')}\n"
        f"  kind: {record.get('kind') or 'unknown'}\n"
        f"  dataset: {record.get('dataset') or 'unknown'}\n"
        f"  args: {record.get('arguments')}\n"
        f"  rows returned: {returned}\n"
        f"  rows in prompt: {shown_label}\n"
        f"  truncated: {'unknown' if truncated is None else truncated}\n"
        f"  columns: {columns}"
    )
    if record.get("kind") == "derived":
        entry += f"\n  formula: {record.get('formula')}"
        if record.get("approximate_datasets"):
            entry += (
                "\n  approximate datasets: "
                + ", ".join(record["approximate_datasets"])
                + " (do not present this ranking as an exact measurement)"
            )
    if shown:
        entry += "\n  data:\n" + "\n".join(
            "    {" + ", ".join(f"{k}: {_clip(v)}" for k, v in row.items()) + "}"
            for row in shown
        )
    return entry


def build_result_catalog(
    records: Sequence[dict], char_budget: int = ANSWER_CATALOG_CHAR_BUDGET
) -> ResultCatalog:
    """Render the tool results for the answer prompt within a character budget.

    Measured rows are never cut to fit the budget. When over budget, example
    rows shrink to ``_MIN_EXAMPLE_ROWS`` one record at a time, newest first. If
    the measured rows alone exceed the budget they are kept anyway and a note is
    returned, so the answer's numbers never come from a silently trimmed table.
    """
    records = [r for r in records if isinstance(r, dict)]
    limits = [
        ANSWER_CONTEXT_ROWS if r.get("kind") == "context" else ANSWER_EXAMPLE_ROWS
        for r in records
    ]
    entries = [_catalog_entry(r, n) for r, n in zip(records, limits)]
    total = sum(len(e) for e in entries) + max(len(entries) - 1, 0)
    for i in reversed(range(len(records))):
        if total <= char_budget:
            break
        if records[i].get("kind") in _ANSWER_MEASURED_KINDS:
            continue
        shrunk = _catalog_entry(records[i], _MIN_EXAMPLE_ROWS)
        total -= len(entries[i]) - len(shrunk)
        entries[i] = shrunk
    notes: tuple[str, ...] = ()
    if total > char_budget:
        note = (
            f"the answer saw all measured rows, but the result catalog is {total} "
            f"characters, over the {char_budget} budget"
        )
        logger.warning("answer catalog over budget: %s", note)
        notes = (note,)
    return ResultCatalog("\n".join(entries), notes)


def dataset_catalog() -> dict:
    """What corpora exist, how big they are, and what each supports.

    Sent to the scope gate so it can judge whether a request names a corpus this
    service can actually serve. Deliberately carries no table, index, or column
    names -- the model selects a dataset *id*, and the registry resolves it.
    """
    from backend.db.datasets import catalog

    return catalog()


def clarify_choices() -> list[str]:
    """What a user can pick: one entry per corpus, never per dataset.

    A corpus read through one dataset is offered by that dataset's id; one read
    through several (OTel: ``otel_s3`` and ``otel_slice``) by its corpus name,
    so a user never has to know about the views -- the agent picks between them.
    """
    from backend.db.datasets import corpora

    return [
        ids[0] if len(ids) == 1 else name for name, ids in corpora().items()
    ]


def clarify_options() -> str:
    """The closed choices offered from the second clarification on.

    Built in code from the registry (select instead of generate), so a choice
    can never name a dataset that does not exist or a subset nobody asked for.
    """
    return f"Pick one: {', '.join(clarify_choices())}, or all of them."


def round_two_question() -> str:
    """The second clarification, fixed in code: the open question already failed
    once, so asking the model again would only reword it."""
    return f"I still can't tell which logs you mean. {clarify_options()}"


def assumed_scope_line(assumed_scope: Sequence[str] | None) -> str:
    """The code-rendered first line of an answer whose scope was assumed.

    Rendered in code, like the cost note, so the model cannot drop it; empty
    when the user chose the scope.
    """
    if not assumed_scope:
        return ""
    others = [d for d in dataset_catalog() if d not in assumed_scope]
    skipped = f"; {', '.join(others)} not searched" if others else ""
    return (
        f"Assumed scope: {', '.join(assumed_scope)} (all local corpora, "
        f"because no dataset was chosen{skipped})."
    )


def assumed_scope_block(assumed_scope: Sequence[str] | None) -> str:
    """The evidence-prompt block restricting an assumed-scope investigation."""
    if not assumed_scope:
        return ""
    return (
        "<assumed scope>\n"
        f"Investigate only these datasets: {', '.join(assumed_scope)}. The user "
        "did not choose a dataset after being asked, so this scope is an "
        "assumption: cover each of them, and say in the summary that the scope "
        "was assumed.\n"
        "</assumed scope>\n"
    )


# The answer rules when no evidence exists in this conversation turn: no tool
# was called and no earlier result is in state, so nothing can be measured.
NO_EVIDENCE_SECTIONS = ("Question this role asks", "Never")
NO_EVIDENCE_RULE = (
    "## No evidence available\n"
    "No log evidence was collected for this request. Answer the latest message "
    "directly from the conversation, briefly. Produce no findings and no "
    "recommendations, and do not write the role's evidence sections (no "
    "'what failed', timeline, counts or priorities): there is nothing measured "
    "to put in them. If the user needs to name a scope, say what they can choose."
)


def has_evidence(state: PipelineState) -> bool:
    """Whether any tool result or tool call is available to ground an answer."""
    return bool(state.get("tool_results")) or bool(state.get("tool_calls"))


def summarized_attempts(tool_results: object, tool_calls: object = None) -> list[dict]:
    """Compact summaries of the tool calls already attempted this turn.

    Built from every call when ``tool_calls`` is known, so the "can an untried
    tool close the gap?" question also sees failed and empty calls. Without it
    (an older state) the successful result records are summarized instead.
    """
    if tool_calls:
        rows = {r.get("id"): r.get("row_count") or 0 for r in tool_results or []}
        summary = []
        for call in tool_calls:
            item = {
                "tool_name": call.get("tool_name"),
                "arguments": call.get("arguments"),
                "status": call.get("status"),
                "row_count": sum(rows.get(rid, 0) for rid in call.get("result_ids", [])),
            }
            if call.get("status") in {"failure", "refused"}:
                item["error_message"] = call.get("error_message")
            summary.append(item)
        return summary
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


def role_context(role: Role) -> str:
    """Who the investigation is for and what that role needs from it."""
    return f"User role: {role['name']}\nRole objective: {role['objective']}\n\n"


def evidence_brief(role: Role, effort: str | None) -> str:
    """What an agent needs to investigate as ``role`` at ``effort``, as prompt blocks.

    The role and its objective, the role's evidence plan (the skill), the team
    ownership mapping, the effort budget and the cost discipline. It depends on
    nothing but the role and the effort, so it is the one place both consumers
    get it from: the chatbot's ``find_evidence`` prompt, and the MCP server's
    role prompts for an external agent. Everything else in ``find_evidence``
    (the request, the datasets, the previous report) is the pipeline's own state.
    """
    spec = get_effort_spec(effort)
    return (
        "<role context>\n"
        f"{role_context(role)}\n"
        "</role context>\n"
        "<role playbook>\n"
        "Follow this evidence plan for the user's role. It decides WHICH "
        "evidence to collect; the effort budget decides how much.\n\n"
        f"{role_playbook(role['name'])}\n"
        "</role playbook>\n"
        "<team ownership>\n"
        f"{ownership_context()}\n"
        "</team ownership>\n"
        "<effort guidance>\n"
        f"Effort level: {effort}.\n"
        f"Tool-call budget: at most {spec.max_tool_calls} tool calls in this investigation (hard limit; to-do updates count).\n"
        f"Hypothesis budget: define at most {spec.max_hypotheses} hypotheses (never more than 3).\n"
        f"Scan budget: at most ~{spec.max_rows_scanned:,} rows read across "
        "the whole investigation. Each tool result reports what it actually "
        "read in query_stats.\n"
        "</effort guidance>\n"
        "<cost discipline>\n"
        f"{COST_DISCIPLINE}\n"
        "</cost discipline>\n"
    )


class PipelinePrompts:
    """Build the prompts used by each stage of the pipeline."""

    shared_grounding = (
        "Use only observed tool results. Separate evidence from interpretation. "
        "Do not invent facts, causes, impact, or missing details. When the report "
        "contains hypotheses, treat supported hypotheses as findings, contradicted "
        "hypotheses as excluded, and unresolved hypotheses as limitations; when it "
        "does not, ground the answer directly in the report's summary and evidence "
        "instead. Results of kind `aggregate`, `trend` or `derived` are measured "
        "numbers; results of kind `search`, `context` or `lookup` are examples and "
        "never establish frequency. Count log entries, never 'incidents'. When a "
        "result is truncated, say that more matching rows exist. The tool result "
        "catalog distinguishes rows returned by a tool from rows in the prompt "
        "sample. Never say rows were not returned, retrieved or displayed merely "
        "because they are absent from the prompt sample; do not describe the "
        "prompt sample as the table displayed to the user. Say rows were outside "
        "the sample instead. Do not claim the contents of unseen rows. Include "
        "relevant time ranges, filters, calculations and evidence references."
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
        return role_context(role)

    def queryable_context(self) -> dict:
        """What can be queried, as named fields.

        One builder for every stage that has to know what a valid target is:
        the scope gate judges against it and the clarifier offers from it, so
        the two can never see different catalogs.
        """
        return {
            "mission": PIPELINE_MISSION,
            "queryable_targets": QUERYABLE_TARGETS,
            "datasets": dataset_catalog(),
            "tool_catalog": TOOL_CATALOG,
        }

    def scope_request(self, state: PipelineState) -> ClassifierRequest:
        # Use raw messages, not the normalizer's rewrite: target wording must
        # remain visible to the scope judgments.
        return {
            "state": {
                "latest_message": state["latest_message"],
                "transcript": state.get("messages", []),
                "latest_answer": state["latest_answer"],
                **self.queryable_context(),
            },
            "questions": {
                "is_log_request": self.is_log_request_question(),
                "has_queryable_scope": self.has_queryable_scope_question(),
                "proceed_without_clarification": (
                    self.proceed_without_clarification_question()
                ),
                "needs_new_evidence": self.needs_new_evidence_question(),
                "is_overview_request": self.is_overview_request_question(),
            },
        }

    def is_overview_request_question(self) -> Noul:
        # Role-independent on purpose: the role never enters the classifier.
        # ``decide_scope`` decides in code that an overview counts as scoped for
        # the team lead and manager (whose default scope is the whole dataset).
        return Noul(
            instructions={
                "question": "Does `latest_message` ask for an overview across all the logs, such as which problems recur most, what is most frequent, or what to prioritize?",
                "inspect": ["`latest_message`", "`transcript`"],
                "focus": "Judge whether the request is about the logs as a whole, not whether it names a specific target.",
            },
            criteria=NoulCriteria(
                true={
                    "what": "The request asks about recurring, most frequent or most important problems across the whole log data.",
                    "examples": ["what recurring problems do we have?", "what should we prioritize?", "which errors happen most often?", "give me an overview of the issues"],
                },
                false={
                    "what": "The request is about one specific failure, event or record, or it is not about the logs.",
                    "not_for": ["why did this request fail?", "what happened at 10:05?", "show me line 42", "greetings or small talk"],
                },
            ),
        )

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
                "attempted_tools": summarized_attempts(state.get("tool_results"), state.get("tool_calls")),
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

    def claims_request(self, targets: Sequence[ClaimTarget]) -> ClassifierRequest:
        """One request judging every measured claim against the results it cites.

        The claims are independent, so they share one call (the questions run in
        parallel and cannot see each other). Each claim carries only its own cited
        results, as ``findings.<key>.claim`` and ``findings.<key>.cited_results``.
        """
        return {
            "state": {
                "findings": {
                    target.key: {
                        "claim": target.statement,
                        "cited_results": [
                            _claim_result(record, target.statement)
                            for record in target.records
                        ],
                    }
                    for target in targets
                }
            },
            "questions": {target.key: self.claim_relation_question(target.key) for target in targets},
        }

    def claim_relation_question(self, key: str) -> Choice:
        claim = f"`findings.{key}.claim`"
        results = f"`findings.{key}.cited_results`"
        return Choice(
            instructions={
                "question": f"How do the cited query results relate to the claim {claim}?",
                "compare": [claim, results],
                "focus": (
                    "Judge only whether the rows shown establish the facts the claim "
                    "states as measured: the values, the entity or component they "
                    "belong to, the window, and the direction of a change. A result "
                    "shows `rows_shown` of `row_count` rows; judge the rows shown. "
                    "Results of kind `search`, `lookup` or `context` are example log "
                    "lines, not counts. Ignore causes and recommendations."
                ),
            },
            criteria={
                "supports": {
                    "what": "The rows show the facts the claim states: the same values for the same entity, window and direction.",
                    "not_for": "Rows that only mention the same entity or topic without showing the stated value or direction.",
                },
                "contradicts": {
                    "what": "The rows show something different from the claim for the same subject: another value, another entity owning the value, or the opposite direction.",
                    "not_for": "Rows about a different subject, window or severity than the claim names, or that do not report the measure the claim is about even for the same entity (those say nothing).",
                    "examples": [
                        "The claim gives 30 rows for component B but the rows give 30 for component A and 4 for B.",
                        "The claim says a count rose but the per-bucket rows fall.",
                    ],
                },
                "says_nothing": {
                    "what": "The rows do not address what the claim asserts, either way.",
                    "not_for": "Rows that show the claim is false (those contradict it).",
                    "examples": [
                        "The claim is about a component or window the rows do not cover.",
                        "The claim states how often something happens but the rows are only example log lines.",
                    ],
                },
            },
        )

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

    def clarify(self, state: PipelineState, *, max_rounds: int) -> str:
        # The clarifier sees the same queryable context the scope gate judged
        # against, plus the code-derived reasons it was called and the round,
        # so it can name real options instead of re-asking the same question.
        reasons = state.get("clarify_reasons") or ["queryable target missing"]
        return (
            "<role context>\n"
            f"{self.role_context(state['role'])}\n"
            "</role context>\n"
            "<queryable context>\n"
            f"{json.dumps(self.queryable_context(), indent=2, default=str)}\n"
            "</queryable context>\n"
            "<missing>\n"
            + "\n".join(f"- {reason}" for reason in reasons)
            + "\n</missing>\n"
            "<clarify round>\n"
            f"clarify round: {state.get('clarify_count', 1)} of {max_rounds}\n"
            "</clarify round>\n"
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

        return (
            evidence_brief(state["role"], state.get("effort", "Medium"))
            + "<datasets>\n"
            f"{dataset_catalog()}\n"
            "</datasets>\n"
            "<request>\n"
            f"{state['normalized']}\n"
            "</request>\n"
            f"{assumed_scope_block(state.get('assumed_scope'))}"
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

        catalog = build_result_catalog(state.get("tool_results", []) or [])
        tool_result_catalog = catalog.text
        role_name = state["role"]["name"]
        if has_evidence(state):
            answer_rules = role_answer_rules(role_name)
        else:
            # Nothing was measured, so the role's evidence sections ("What
            # failed", "Timeline", ...) would only be filled with "nothing".
            answer_rules = (
                f"{NO_EVIDENCE_RULE}\n\n"
                f"{role_answer_rules(role_name, NO_EVIDENCE_SECTIONS)}"
            )

        return (
            "<grounding rules>\n"
            f"{self.shared_grounding}\n\n"
            "</grounding rules>\n"
            "<role context>\n"
            f"{self.role_context(state['role'])}\n"
            "</role context>\n"
            "<role answer rules>\n"
            f"{answer_rules}\n"
            "</role answer rules>\n"
            "<team ownership>\n"
            f"{ownership_context()}\n"
            "</team ownership>\n"
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
