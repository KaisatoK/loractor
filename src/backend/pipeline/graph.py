from langgraph.graph import StateGraph, END
from langchain_typesafe import TypeSafeClassifier
from langchain_ollama import ChatOllama

from typing import TypedDict, Literal

from src.backend.pipeline.agents import retrieval_agent, search_agent
from src.backend.pipeline.pipeline_prompts import PipelinePrompts


class Role(TypedDict):
    name: Literal["engineer", "team lead", "manager"]
    objective: str


class NormalizedOutput(TypedDict):
    intent: Literal["count", "trend", "compare", "search", "unknown"]
    explanation: str


class PlanStep(TypedDict):
    tool: Literal[
        "count_by_category",
        "get_trend",
        "compare_periods",
        "search_logs",
        "top_locations",
    ]
    instruction: str


class ExecutionPlan(TypedDict):
    steps: list[PlanStep]


class PipelineRoute(TypedDict):
    next_state: Literal["plan", "answer", "conclude"]
    pass_probability: float
    probabilities: dict[str, float]
    revision_reason: str


class PipelineState(TypedDict):
    question: str
    role: Role
    normalized: NormalizedOutput
    plan: ExecutionPlan
    plan_history: list[ExecutionPlan]
    evidence: list
    confidence: PipelineRoute
    revision_feedback: str
    answer: str


# ROLE_OBJECTIVES = {
#     "engineer": "Investigate an error and find the relevant evidence.",
#     "team lead": "Understand recurring problems and patterns.",
#     "manager": "Decide what to prioritize based on the findings.",
# }

llm = ChatOllama(model="qwen3.5:4b")

router = TypeSafeClassifier()

prompts = PipelinePrompts()


def normalize_node(state: PipelineState) -> PipelineState:
    response = llm.with_structured_output(NormalizedOutput).invoke(
        prompts.normalize(state)
    )
    return {**state, "normalized": response}


def plan_node(state: PipelineState) -> PipelineState:
    plan_history = list(state.get("plan_history", []))
    if state.get("plan"):
        plan_history.append(state["plan"])

    plan = llm.with_structured_output(ExecutionPlan).invoke(prompts.plan(state))
    return {
        **state,
        "plan": plan,
        "plan_history": plan_history,
        "evidence": [],
        "answer": "",
    }


def execute_plan_node(state: PipelineState) -> PipelineState:
    evidence = list(state.get("evidence", []))

    retrieval_tools = {"count_by_category", "get_trend", "compare_periods"}
    search_tools = {"search_logs", "top_locations"}

    for step in state["plan"]["steps"]:
        tool_name = step["tool"]
        agent = (
            retrieval_agent
            if tool_name in retrieval_tools
            else search_agent if tool_name in search_tools else None
        )

        if agent is None:
            raise ValueError(f"Unsupported planned tool: {tool_name}")

        result = agent.invoke(
            {"messages": [("user", prompts.execute_tool(state, step))]}
        )
        evidence.append({"step": step, "result": result})

    return {**state, "evidence": evidence}


def answer_node(state: PipelineState) -> PipelineState:
    answer = llm.invoke(prompts.answer(state))
    return {**state, "answer": answer.content}


def confidence_node(state: PipelineState) -> PipelineState:
    response = router.invoke(prompts.confidence_request(state))

    route_answer = response.choices["next_state"]
    next_state = max(route_answer.probabilities, key=route_answer.probabilities.get)
    reason_answer = response.choices["revision_reason"]
    revision_reason = max(
        reason_answer.probabilities, key=reason_answer.probabilities.get
    )

    revision_feedback = {
        "missing_evidence": "Add or change tool calls to collect the missing evidence.",
        "irrelevant_evidence": "Change the search criteria or tools so the evidence directly addresses the question.",
        "unsupported_answer": "Keep the useful evidence, but revise the answer so every claim is supported.",
        "role_mismatch": "Adjust the evidence plan and answer to the user's role and decision needs.",
        "none": "No revision is required.",
    }[revision_reason]

    # Debug output while the confidence policy is being tuned.
    print(
        f"confidence pass={response.nouls['pass'].noul:.3f} "
        f"next_state={next_state} next_state_confidence={route_answer.confidence:.3f} "
        f"revision_reason={revision_reason} "
        f"revision_reason_confidence={reason_answer.confidence:.3f}"
    )

    return {
        **state,
        "confidence": {
            "next_state": next_state,
            "pass_probability": response.nouls["pass"].noul,
            "probabilities": route_answer.probabilities,
            "revision_reason": revision_reason,
        },
        "revision_feedback": revision_feedback,
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
