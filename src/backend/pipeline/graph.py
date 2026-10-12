"""LangGraph pipeline: normalize → scope → (clarify | general | answer) → confidence.

The scope and confidence routers are TypeSafe classifiers (see ``decision.py``):
``scope`` picks one action-oriented route and ``confidence`` decides whether to
return the answer or revise it.
"""

import logging
from typing import Literal

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, StateGraph

from backend.pipeline.agents import (
    answer_agent,
    clarify_agent,
    evidence_report_agent,
    general_agent,
    get_find_evidence_agent,
    normalize_agent,
)
from backend.pipeline.answers import serialize_answer
from backend.pipeline.decision import (
    FOLLOW_UP_THRESHOLD,
    classifier,
    decide_confidence,
    decide_scope,
)
from backend.pipeline.prompts import PipelinePrompts, normalized_is_usable
from backend.pipeline.state import PipelineState
from backend.pipeline.tools.results import collect_tool_results

logger = logging.getLogger(__name__)

prompts = PipelinePrompts()

# Human-readable revision feedback for each revision_reason label the confidence
# classifier can return. It is fed to the find_evidence/answer prompts on a
# revision so those agents know what to change.
REVISION_FEEDBACK = {
    "missing_evidence": "More evidence is required.",
    "irrelevant_evidence": (
        "The collected evidence does not address the important hypotheses."
    ),
    "unsupported_answer": (
        "The evidence is adequate, but the answer misuses or omits it."
    ),
    "role_mismatch": "The answer does not support the user's role objective.",
    "none": "No revision is required.",
}
NO_REVISION = "No revision is required."


def normalize_node(state: PipelineState) -> PipelineState:
    prompt = prompts.normalize(state)
    result = normalize_agent.invoke(
        {
            "messages": [
                {"role": "user", "content": prompt},
            ]
        }
    )
    normalized = result["structured_response"].model_dump()

    # A failed model call yields the empty fallback shape; never overwrite a
    # valid previous normalization with it. A new turn also resets the revision
    # budget consumed by the confidence loop.
    previous = state.get("normalized")
    if not normalized_is_usable(normalized) and normalized_is_usable(previous):
        return {**state, "normalized": previous, "revision_count": 0}

    return {**state, "normalized": normalized, "revision_count": 0}


def scope_node(state: PipelineState) -> PipelineState:
    clarify_count = state.get("clarify_count", 0)
    try:
        response = classifier.invoke(prompts.scope_request(state))
        scope = decide_scope(response, clarify_count)
        route = response.choices.get("route")
        logger.info(
            "scope=%s needs_clarification=%.3f route=%s route_confidence=%s clarify_count=%d",
            scope,
            response.nouls["needs_clarification"].noul,
            getattr(route, "choice", "?"),
            f"{route.confidence:.3f}" if route is not None else "?",
            clarify_count,
        )
        logger.debug("scope probabilities: %s", getattr(route, "probabilities", {}))
    except Exception as error:  # noqa: BLE001 - a classifier failure must not break the run
        logger.warning("scope classifier failed, investigating instead: %s", error)
        scope = "find_evidence"

    # Track consecutive clarification turns so a vague back-and-forth cannot ask
    # forever. Any non-clarify route clears the count.
    if scope == "clarify":
        clarify_count += 1
    else:
        clarify_count = 0

    return {**state, "scope": scope, "clarify_count": clarify_count}


def clarify_node(state: PipelineState) -> PipelineState:
    result = clarify_agent.invoke(
        {
            "messages": [
                {"role": "user", "content": prompts.clarify(state)},
            ]
        }
    )
    question = result["messages"][-1].text
    logger.info("clarify: asking the user for more information")
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


def find_evidence_node(state: PipelineState) -> PipelineState:
    result = get_find_evidence_agent(state.get("effort")).invoke(
        {
            "messages": [
                {"role": "user", "content": prompts.find_evidence(state)},
            ]
        }
    )

    investigation = "\n".join(
        f"{message.type}: {message.content}" for message in result["messages"]
    )

    report_result = evidence_report_agent.invoke(
        {
            "messages": [
                {
                    "role": "user",
                    "content": (
                        f"{prompts.find_evidence(state)}\n"
                        "<investigation transcript>\n"
                        f"{investigation}\n"
                        "</investigation transcript>"
                    ),
                }
            ]
        }
    )

    evidence_report = report_result["structured_response"].model_dump()
    tool_results = collect_tool_results(result["messages"])
    history = [*(state.get("evidence_report_history") or []), evidence_report]

    return {
        **state,
        "evidence_report": evidence_report,
        "tool_results": tool_results,
        "answer": "",
        "evidence_report_history": history,
    }


def answer_node(state: PipelineState) -> PipelineState:
    result = answer_agent.invoke(
        {
            "messages": [
                {"role": "user", "content": prompts.answer(state)},
            ]
        }
    )
    structured = result["structured_response"]
    valid_ids = {record.get("id") for record in state.get("tool_results") or []}
    answer = serialize_answer(structured, valid_ids)
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
            "pass_probability": 1.0,
            "probabilities": {"find_evidence": 0.0, "answer": 0.0, "end": 1.0},
            "revision_reason": "none",
            "worth_follow_up": 0.0,
        }

    next_state = route["next_state"]
    logger.info(
        "confidence=%s pass=%.3f reason=%s worth_follow_up=%.3f revision_count=%d",
        next_state,
        route["pass_probability"],
        route["revision_reason"],
        route.get("worth_follow_up", 0.0),
        revision_count,
    )
    confidence = {
        "next_state": next_state,
        "pass_probability": route["pass_probability"],
        "probabilities": route["probabilities"],
        "revision_reason": route["revision_reason"],
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

checkpointer = InMemorySaver()
chatbot = graph.compile(checkpointer=checkpointer)
