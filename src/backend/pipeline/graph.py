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
from backend.pipeline.effort import get_effort_spec
from backend.pipeline.pipeline_prompts import PipelinePrompts, normalized_is_usable
from backend.pipeline.schema.schemas import PipelineState
from backend.pipeline.utils import collect_tool_results

# router = TypeSafeClassifier()

prompts = PipelinePrompts()
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
    # response = router.invoke(prompts.scope_request(state))

    # pipeline_probability = response.nouls["requires_pipeline"].noul

    # route = "pipeline" if pipeline_probability > SCOPE_CONFIDENCE_THRESHOLD else "general"

    # return {
    #     **state,
    #     "scope": route,
    # }

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

    # print("@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@")
    # print("general answer:", answer)
    # print("@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@")
    return {**state, "answer": answer}


def find_evidence_node(state: PipelineState) -> PipelineState:
    evidence_report_history = list(state.get("evidence_report_history", []))
    # if state.get("evidence_report"):
    #     find_evidence_history.append(
    #         {
    #             ## ...
    #         }
    #     )

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
        "evidence_report_history": evidence_report_history,
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
    answer = result["messages"][-1].text

    print("@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@")
    print("answer:", answer)
    print("@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@")
    return {**state, "answer": answer}


def confidence_node(state: PipelineState) -> PipelineState:
    # response = router.invoke(prompts.confidence_request(state))

    # route_answer = response.choices["next_state"]
    # next_state = max(route_answer.probabilities, key=route_answer.probabilities.get)
    # reason_answer = response.choices["revision_reason"]
    # revision_reason = max(
    #     reason_answer.probabilities, key=reason_answer.probabilities.get
    # )

    # revision_feedback = {
    #     "missing_evidence": "Add or change tool calls to collect the missing evidence.",
    #     "irrelevant_evidence": "Change the search criteria or tools so the evidence directly addresses the question.",
    #     "unsupported_answer": "Keep the useful evidence, but revise the answer so every claim is supported.",
    #     "role_mismatch": "Adjust the evidence plan and answer to the user's role and decision needs.",
    #     "none": "No revision is required.",
    # }[revision_reason]

    # # Debug output while the confidence policy is being tuned.
    # print(
    #     f"confidence pass={response.nouls['pass'].noul:.3f} "
    #     f"next_state={next_state} next_state_confidence={route_answer.confidence:.3f} "
    #     f"revision_reason={revision_reason} "
    #     f"revision_reason_confidence={reason_answer.confidence:.3f}"
    # )

    # return {
    #     **state,
    #     "confidence": {
    #         "next_state": next_state,
    #         "pass_probability": response.nouls["pass"].noul,
    #         "probabilities": route_answer.probabilities,
    #         "revision_reason": revision_reason,
    #     },
    #     "revision_feedback": revision_feedback,
    # }
    # When enabled, gate on the effort spec:
    # spec = get_effort_spec(state["effort"])
    # accept = pass_probability >= spec.answer_confidence_threshold
    # loop again only while iteration_count < spec.max_iterations
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
