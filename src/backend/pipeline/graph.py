"""LangGraph pipeline: normalize → scope → (clarify | general | answer) → confidence.

Scope and confidence use TypeSafe (Jev) for independent semantic judgments.
The deterministic policies in ``decision.py`` compose those Noul probabilities
and own every route and bounded revision transition.
"""

import json
import logging
import os
import sqlite3
from pathlib import Path
from typing import Literal

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.config import get_stream_writer
from langgraph.graph import END, StateGraph

from backend.pipeline.agents import (
    generate_answer,
    clarify_agent,
    general_agent,
    get_find_evidence_agent,
    generate_normalized,
)
from backend.pipeline.agents.evidence_report import generate_evidence_judgement
from backend.pipeline.answers import serialize_answer
from backend.pipeline.contracts import EvidenceReport
from backend.pipeline.data_scope import build_data_scope
from backend.pipeline.evidence import build_evidence
from backend.pipeline.playbooks import team_for
from backend.pipeline.priority import with_priority
from backend.pipeline.cost import investigation_cost
from backend.pipeline.decision import (
    FOLLOW_UP_THRESHOLD,
    MAX_CLARIFY_ROUNDS,
    clarify_reasons,
    classifier,
    decide_confidence,
    decide_scope,
    scope_assumption,
)
from backend.db.datasets import local_datasets
from backend.pipeline.prompts import PipelinePrompts, normalized_is_usable
from backend.pipeline.prompts.builders import (
    assumed_scope_line,
    build_result_catalog,
    round_two_question,
)
from backend.pipeline.state import PipelineState
from backend.pipeline.tools.results import collect_tool_calls, collect_tool_results

logger = logging.getLogger(__name__)

prompts = PipelinePrompts()

# Human-readable revision feedback for each revision_reason label the confidence
# classifier can return. It is fed to the find_evidence/answer prompts on a
# revision so those agents know what to change.
REVISION_FEEDBACK = {
    "missing_evidence": "More evidence is required.",
    "irrelevant_evidence": "The collected evidence does not address the request.",
    "unsupported_answer": (
        "The evidence is adequate, but the answer misuses or omits it."
    ),
    "role_mismatch": "The answer does not support the user's role objective.",
    "incomplete_answer": "The answer does not resolve the request or state its limitations.",
    "none": "No revision is required.",
}
NO_REVISION = "No revision is required."

# Roles whose answers get the code-computed priority ranking.
PRIORITY_ROLES = frozenset({"team_lead", "manager"})


def _emit_stage(name: str) -> None:
    """Announce a pipeline node's stage to the live progress panel."""
    try:
        get_stream_writer()({"type": "node_stage", "name": name})
    except RuntimeError:
        pass  # node invoked outside a runnable context (direct calls in tests)


def normalize_node(state: PipelineState) -> PipelineState:
    _emit_stage("normalize")
    prompt = prompts.normalize(state)
    normalized = generate_normalized(prompt).model_dump()

    # Never overwrite a valid previous normalization with an unusable or empty
    # one. A new turn also resets the revision budget consumed by the confidence
    # loop.
    previous = state.get("normalized")
    if not normalized_is_usable(normalized) and normalized_is_usable(previous):
        return {**state, "normalized": previous, "revision_count": 0}

    return {**state, "normalized": normalized, "revision_count": 0}


def scope_node(state: PipelineState) -> PipelineState:
    clarify_count = state.get("clarify_count", 0)
    forced = state.get("forced_scope")
    if forced:
        # A role switch re-investigates an already-answered question for
        # another role: the user has authorized it, so no clarification.
        logger.info("scope=%s (forced by role switch)", forced)
        return {
            **state,
            "scope": forced,
            "clarify_count": 0,
            "scope_signals": {},
            "clarify_reasons": [],
            "assumed_scope": None,
        }
    signals: dict[str, float] = {}
    reasons: list[str] = []
    assumed = False
    try:
        response = classifier.invoke(prompts.scope_request(state))
        role_name = (state.get("role") or {}).get("name")
        scope = decide_scope(response, clarify_count, role_name)
        # Raw judgments are kept next to the code-derived policy outputs, so a
        # wrong route can be traced to the input, the model or the code.
        signals = {name: answer.noul for name, answer in response.nouls.items()}
        reasons = clarify_reasons(response, role_name)
        assumed = scope == "find_evidence" and scope_assumption(
            response, clarify_count, role_name
        )
        logger.info(
            "scope=%s role=%s signals=%s reasons=%s assumed_scope=%s clarify_count=%d model=%s",
            scope,
            role_name,
            {name: round(value, 3) for name, value in signals.items()},
            reasons,
            assumed,
            clarify_count,
            response.model,
        )
    except Exception as error:  # noqa: BLE001 - a classifier failure must not break the run
        logger.warning("scope classifier failed, investigating instead: %s", error)
        scope = "find_evidence"

    # Track consecutive clarification turns so a vague back-and-forth cannot ask
    # forever. Any non-clarify route clears the count.
    if scope == "clarify":
        clarify_count += 1
    else:
        clarify_count = 0

    return {
        **state,
        "scope": scope,
        "clarify_count": clarify_count,
        "scope_signals": signals,
        "clarify_reasons": reasons,
        # The budget ran out without a scope: investigate every local corpus
        # (the remote tiers stay opt-in) and say so in the answer.
        "assumed_scope": [ds.id for ds in local_datasets()] if assumed else None,
    }


def clarify_node(state: PipelineState) -> PipelineState:
    round_number = state.get("clarify_count", 1)
    if round_number >= 2:
        # The open question already failed once: from the second round the user
        # gets one fixed closed question, built in code from the registry, so
        # the model cannot reword the same question or guess a subset.
        question = round_two_question()
    else:
        result = clarify_agent.invoke(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": prompts.clarify(state, max_rounds=MAX_CLARIFY_ROUNDS),
                    },
                ]
            }
        )
        question = result["messages"][-1].text
    logger.info(
        "clarify: asking the user for more information (round %d of %d, reasons=%s)",
        round_number,
        MAX_CLARIFY_ROUNDS,
        state.get("clarify_reasons"),
    )
    return {**state, "answer": question}


def general_node(state: PipelineState) -> PipelineState:
    result = general_agent.invoke(
        {
            "messages": [
                {"role": "user", "content": state["conversation"]},
            ]
        }
    )

    answer = result["messages"][-1].text
    return {**state, "answer": answer}


def _last_agent_summary(messages) -> str:
    """The investigator's final summary: the last non-empty ``AIMessage`` text
    walking the transcript backwards (empty string when there is none).
    """
    for message in reversed(messages):
        if getattr(message, "type", "") != "ai":
            continue
        text = getattr(message, "text", "") or ""
        if isinstance(text, str) and text.strip():
            return text
    return ""


def find_evidence_node(state: PipelineState) -> PipelineState:
    result = get_find_evidence_agent(state.get("effort")).invoke(
        {
            "messages": [
                {"role": "user", "content": prompts.find_evidence(state)},
            ]
        }
    )

    # Raw rows stay on the agent's transcript (DB tools run on the main agent),
    # collected from its messages. Each record's inline `R{n}` id/`row_count`
    # feeds both the report catalog and answer tables, so the ids stay identical.
    records = collect_tool_results(result["messages"])

    # The evidence items come from the actual tool calls, in code; the model
    # (a direct DeepSeek call, no structured-output wrapper) writes only the
    # summary and hypotheses over them. A provider or validation failure
    # propagates rather than being swallowed.
    calls = collect_tool_calls(result["messages"], records)
    evidence = build_evidence(calls, records)
    role = state["role"]
    judgement = generate_evidence_judgement(
        request=json.dumps(state["normalized"], default=str),
        role=f"{role['name']}: {role['objective']}",
        investigation=_last_agent_summary(result["messages"]),
        evidence=evidence,
        results=records,
    )
    # The data scope is derived from the records in code, never by the model.
    evidence_report = EvidenceReport(
        summary=judgement.summary,
        evidence=evidence,
        hypotheses=judgement.hypotheses,
        coverage=judgement.coverage,
        data_scope=build_data_scope(records),
    ).model_dump()
    history = [*(state.get("evidence_report_history") or []), evidence_report]

    return {
        **state,
        "evidence_report": evidence_report,
        "tool_results": records,
        "tool_calls": calls,
        "answer": "",
        "evidence_report_history": history,
    }


def answer_node(state: PipelineState) -> PipelineState:
    _emit_stage("answer")
    records = state.get("tool_results") or []
    # Team leads and managers rank problems: the ranking is arithmetic over the
    # measured counts, so it is computed here in code (pipeline/priority.py) and
    # added as a derived result the answer can cite. Idempotent on revisions.
    if state["role"]["name"] in PRIORITY_ROLES:
        records = with_priority(records, team_for)
    state = {**state, "tool_results": records}
    prompt = prompts.answer(state)
    logger.info("answer prompt: %d chars", len(prompt))
    structured = generate_answer(prompt)
    valid_ids = {record.get("id") for record in records}
    notes = build_result_catalog(records).notes
    # Cost is measured from the tool results' own query_stats, not narrated by
    # the model, so the volume the answer reports cannot be wrong.
    cost = investigation_cost(records)
    answer = serialize_answer(
        structured, valid_ids, records, notes, cost_note=cost.get("note", "")
    )
    # Rebuilt from the structured answer on every revision, so the line is
    # present exactly once.
    assumption = assumed_scope_line(state.get("assumed_scope"))
    if assumption:
        answer = f"{assumption}\n\n{answer}"
    # The answer agent drafts the optional follow-up; confidence decides whether
    # to show it, so keep the draft in state either way.
    if isinstance(structured, dict):
        follow_up = structured.get("follow_up")
    else:
        follow_up = getattr(structured, "follow_up", None)
    follow_up = follow_up or None
    logger.info(
        "answer drafted (%d chars, follow_up=%s)", len(answer), follow_up is not None
    )
    logger.debug("answer: %s", answer)
    return {**state, "answer": answer, "follow_up": follow_up}


def confidence_node(state: PipelineState) -> PipelineState:
    revision_count = state.get("revision_count", 0)
    try:
        response = classifier.invoke(prompts.confidence_request(state))
        route = decide_confidence(response, state.get("effort"), revision_count)
    except Exception as error:  # noqa: BLE001 - a classifier failure must not break the run
        logger.warning("confidence classifier failed, returning answer: %s", error)
        route = {
            "next_state": "end",
            "revision_reason": "none",
            "signals": {},
            "worth_follow_up": 0.0,
        }

    next_state = route["next_state"]
    logger.info(
        "confidence=%s reason=%s worth_follow_up=%.3f revision_count=%d model=%s signals=%s",
        next_state,
        route["revision_reason"],
        route.get("worth_follow_up", 0.0),
        revision_count,
        getattr(response, "model", "classifier-failed") if "response" in locals() else "classifier-failed",
        route.get("signals", {}),
    )
    confidence = {
        "next_state": next_state,
        "revision_reason": route["revision_reason"],
        "signals": route.get("signals", {}),
    }

    # Only a final (non-revising) answer gets the drafted follow-up question, and
    # only when the classifier judged it materially useful.
    answer = state.get("answer", "")
    if next_state == "end" and route.get("worth_follow_up", 0.0) >= FOLLOW_UP_THRESHOLD:
        follow_up = (state.get("follow_up") or "").strip()
        if follow_up:
            answer = f"{answer}\n\n{follow_up}".strip()
            logger.info("confidence: appended follow-up question")

    updated = {
        **state,
        "confidence": confidence,
        "revision_feedback": REVISION_FEEDBACK.get(
            route["revision_reason"], NO_REVISION
        ),
        "answer": answer,
    }
    if next_state != "end":
        updated["revision_count"] = revision_count + 1
    return updated


def route_after_scope(
    state: PipelineState,
) -> Literal["clarify", "general", "direct_answer", "find_evidence"]:
    return state["scope"]


def route_after_confidence(
    state: PipelineState,
) -> Literal["find_evidence", "answer", "end"]:
    return state["confidence"]["next_state"]


graph = StateGraph(PipelineState)
graph.add_node("normalize", normalize_node)
graph.add_node("scope", scope_node)
graph.add_node("clarify", clarify_node)
graph.add_node("general", general_node)
graph.add_node("find_evidence", find_evidence_node)
graph.add_node("answer", answer_node)
graph.add_node("confidence", confidence_node)

graph.set_entry_point("normalize")

graph.add_edge("normalize", "scope")
graph.add_conditional_edges(
    "scope",
    route_after_scope,
    {
        "clarify": "clarify",
        "general": "general",
        "direct_answer": "answer",
        "find_evidence": "find_evidence",
    },
)
graph.add_edge("clarify", END)
graph.add_edge("general", END)
graph.add_edge("find_evidence", "answer")
graph.add_edge("answer", "confidence")
graph.add_conditional_edges(
    "confidence",
    route_after_confidence,
    {"find_evidence": "find_evidence", "answer": "answer", "end": END},
)


def build_checkpointer():
    """Checkpointer backend, selected by ``CHECKPOINT_BACKEND``: ``sqlite``
    (default; durable, lock-serialized, thread-safe), ``memory`` (run-local, not
    durable), ``postgres`` (durable, shareable; needs dep + POSTGRES_URL)."""
    backend = os.getenv("CHECKPOINT_BACKEND", "sqlite").lower()

    if backend == "memory":
        logger.info("Using in-memory checkpointer (state is not durable).")
        return InMemorySaver()

    if backend == "sqlite":
        from langgraph.checkpoint.sqlite import SqliteSaver

        db_path = Path(
            os.getenv(
                "CHECKPOINT_SQLITE_PATH",
                str(Path.cwd() / ".data" / "checkpoints.sqlite"),
            )
        )
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path), check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        saver = SqliteSaver(conn)
        saver.setup()
        logger.info("Using SQLite checkpointer at %s", db_path)
        return saver

    if backend == "postgres":
        try:
            from langgraph.checkpoint.postgres import PostgresSaver
            from psycopg_pool import ConnectionPool
        except ImportError as e:  # pragma: no cover - optional dependency
            raise RuntimeError(
                "CHECKPOINT_BACKEND=postgres requires 'langgraph-checkpoint-postgres' "
                "and 'psycopg_pool'. Install them, or use CHECKPOINT_BACKEND=sqlite."
            ) from e

        url = os.getenv("CHECKPOINT_POSTGRES_URL")
        if not url:
            raise RuntimeError(
                "CHECKPOINT_BACKEND=postgres requires CHECKPOINT_POSTGRES_URL."
            )
        pool = ConnectionPool(conninfo=url, max_size=10, open=True)
        saver = PostgresSaver(pool)
        saver.setup()
        logger.info("Using Postgres checkpointer.")
        return saver

    raise ValueError(
        f"Unknown CHECKPOINT_BACKEND={backend!r}; expected memory, sqlite, or postgres."
    )


checkpointer = build_checkpointer()
chatbot = graph.compile(checkpointer=checkpointer)
