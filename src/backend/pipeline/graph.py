from langgraph.graph import StateGraph, END
from langchain_typesafe import TypeSafeClassifier

from typing import Literal

from backend.pipeline.agents import (
    answer_agent,
    evidence_agent,
    executor_agent,
    normalize_agent,
    plan_agent,
)
from backend.pipeline.pipeline_prompts import PipelinePrompts
from backend.pipeline.schemas import NormalizedOutput, PipelineRoute, PipelineState

# router = TypeSafeClassifier()

prompts = PipelinePrompts()


def normalize_node(state: PipelineState) -> PipelineState:
    result = normalize_agent.invoke(
        {
            "messages": [
                {"role": "user", "content": prompts.normalize(state)},
            ]
        }
    )
    normalized = result["structured_response"]
    return {**state, "normalized": normalized.model_dump()}


def plan_node(state: PipelineState) -> PipelineState:
    plan_history = list(state.get("plan_history", []))
    if state.get("plan"):
        plan_history.append(
            {
                "plan": state["plan"],
                "evidence": state.get("evidence", {}),
                "answer": state.get("answer", ""),
                "confidence": state.get("confidence"),
            }
        )

    result = plan_agent.invoke(
        {
            "messages": [
                {"role": "user", "content": prompts.plan(state)},
            ]
        }
    )
    plan = result["structured_response"]
    plan_data = plan.model_dump()
    return {
        **state,
        "plan": plan_data,
        "plan_history": plan_history,
        "evidence": {},
        "answer": "",
    }


def execute_plan_node(state: PipelineState) -> PipelineState:
    result = executor_agent.invoke(
        {
            "messages": [
                {"role": "user", "content": prompts.execute_evidence(state)},
            ]
        }
    )

    tool_results = []
    for message in result.get("messages", []):
        if getattr(message, "type", None) != "tool":
            continue
        tool_results.append(
            {
                "source_tool": getattr(message, "name", "unknown"),
                "content": message.content,
                "tool_call_id": getattr(message, "tool_call_id", None),
            }
        )

    evidence_result = evidence_agent.invoke(
        {
            "messages": [
                {
                    "role": "user",
                    "content": (
                        f"Investigation plan: {state['plan']}\n\n"
                        f"Tool results: {tool_results}"
                    ),
                }
            ]
        }
    )
    evidence_report = evidence_result["structured_response"].model_dump()
    return {
        **state,
        "evidence": {
            "report": evidence_report,
            "tool_results": tool_results,
        },
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
    print("@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@")
    print("answer:", state["answer"])
    print("@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@")
    return {
        **state,
        "confidence": {
            "next_state": "conclude",
            "pass_probability": 1,
            "probabilities": {
                "plan": 0,
                "answer": 0,
                "conclude": 1,
            },
            "revision_reason": "No revision is required.",
        },
        "revision_feedback": "No revision is required.",
    }


def route_after_confidence(
    state: PipelineState,
) -> Literal["plan", "answer", "conclude"]:
    return state["confidence"]["next_state"]


graph = StateGraph(PipelineState)
graph.add_node("normalize", normalize_node)
graph.add_node("plan", plan_node)
graph.add_node("execute_plan", execute_plan_node)
graph.add_node("answer", answer_node)
graph.add_node("confidence", confidence_node)

graph.set_entry_point("normalize")
graph.add_edge("normalize", "plan")
graph.add_edge("plan", "execute_plan")
graph.add_edge("execute_plan", "answer")
graph.add_edge("answer", "confidence")
graph.add_conditional_edges(
    "confidence",
    route_after_confidence,
    {"plan": "plan", "answer": "answer", "conclude": END},
)

chatbot = graph.compile()
