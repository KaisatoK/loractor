"""LangGraph pipeline: normalize → scope → (general | find_evidence → answer → confidence)."""

import logging
from typing import Literal

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, StateGraph

from backend.pipeline.agents import (
    answer_agent,
    evidence_report_agent,
    general_agent,
    get_find_evidence_agent,
    normalize_agent,
)
from backend.pipeline.answers import serialize_answer
from backend.pipeline.prompts import PipelinePrompts, normalized_is_usable
from backend.pipeline.state import PipelineState
from backend.pipeline.tools.results import collect_tool_results

logger = logging.getLogger(__name__)

prompts = PipelinePrompts()

# Reserved for the scope classifier: probability above which a request is routed
# into the evidence pipeline rather than to the general agent. Used once the
# scope_node stub below is replaced with the TypeSafe classifier.
SCOPE_CONFIDENCE_THRESHOLD = 0.64


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
    # valid previous normalization with it.
    previous = state.get("normalized")
    if not normalized_is_usable(normalized) and normalized_is_usable(previous):
        return {**state, "normalized": previous}

    return {**state, "normalized": normalized}


def scope_node(state: PipelineState) -> PipelineState:
    # TODO(scope routing): classify the request with prompts.scope_request(state)
    # and route to "pipeline"/"general" using SCOPE_CONFIDENCE_THRESHOLD. Until
    # then every request is treated as unresolved and goes to find_evidence.
    return {
        **state,
        "scope": "unresolved",
    }


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

    return {
        **state,
        "evidence_report": evidence_report,
        "tool_results": tool_results,
        "answer": "",
    }


def answer_node(state: PipelineState) -> PipelineState:
    result = answer_agent.invoke(
        {
            "messages": [
                {"role": "user", "content": prompts.answer(state)},
            ]
        }
    )
    valid_ids = {record.get("id") for record in state.get("tool_results") or []}
    answer = serialize_answer(result["structured_response"], valid_ids)
    logger.debug("answer: %s", answer)
    return {**state, "answer": answer}


def confidence_node(state: PipelineState) -> PipelineState:
    # TODO(confidence routing): score the answer with prompts.pass_question /
    # next_state_question / revision_reason_question and, when it fails, set
    # confidence["next_state"] to "find_evidence" or "answer" to trigger a
    # revision, gated by the effort spec's threshold and iteration budget.
    return {
        **state,
        "confidence": {
            "next_state": "end",
            "pass_probability": 1,
            "probabilities": {
                "find_evidence": 0,
                "answer": 0,
                "end": 1,
            },
            "revision_reason": "No revision is required.",
        },
        "revision_feedback": "No revision is required.",
    }


def route_after_scope(
    state: PipelineState,
) -> Literal["irrelevant", "resolved", "unresolved"]:
    return state["scope"]


def route_after_confidence(
    state: PipelineState,
) -> Literal["find_evidence", "answer", "end"]:
    return state["confidence"]["next_state"]


graph = StateGraph(PipelineState)
graph.add_node("normalize", normalize_node)
graph.add_node("scope", scope_node)
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
        "irrelevant": "general",
        "resolved": "answer",
        "unresolved": "find_evidence",
    },
)
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
