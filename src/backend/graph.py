from langgraph.graph import StateGraph, END
from langgraph.graph.message import add_messages
from langchain_typesafe import Choice, Noul, Score, TypeSafeClassifier
from langchain_ollama import ChatOllama

from typing import TypedDict, Annotated, Literal

from backend.agents import retrieval_agent, search_agent

llm = ChatOllama(model="qwen3.5:4b")

router = TypeSafeClassifier()

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
    role: str
    normalized: NormalizedOutput
    plan: ExecutionPlan
    plan_history: list[ExecutionPlan]
    evidence: list
    confidence: PipelineRoute
    revision_feedback: str
    answer: str
    
def normalize_node(state: PipelineState) -> PipelineState:
    response = llm.with_structured_output(NormalizedOutput).invoke(
        f"Role: {state['role']}. Question: {state['question']}. "
        f"Classify intent and extract parameters as structured JSON."
    )
    return {**state, "normalized": response}


def plan_node(state: PipelineState) -> PipelineState:
    previous_plan = state.get("plan")
    revision_feedback = state.get("revision_feedback", "No previous attempt exists.")
    plan_history = list(state.get("plan_history", []))
    if previous_plan:
        plan_history.append(previous_plan)

    plan = llm.with_structured_output(ExecutionPlan).invoke(
        f"Role: {state['role']}. Question: {state['question']}. "
        f"Normalized request: {state['normalized']}. "
        f"Previous plan: {previous_plan or 'None'}. "
        f"Previous plan history: {plan_history}. "
        f"Revision feedback: {revision_feedback}. "
        "Create a plan of every tool call needed to gather sufficient evidence. "
        "Use multiple steps when the question requires multiple types of evidence. "
        "Each step must name one tool and provide its exact instruction. "
        "If this is a revision, change the approach to address the feedback and do not "
        "repeat the previous plan unchanged."
    )
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
            else search_agent
            if tool_name in search_tools
            else None
        )

        if agent is None:
            raise ValueError(f"Unsupported planned tool: {tool_name}")

        result = agent.invoke(
            {
                "messages": [
                    (
                        "user",
                        f"Original question: {state['question']}\n"
                        f"Normalized request: {state['normalized']}\n"
                        f"Execute this planned tool: {tool_name}\n"
                        f"Instruction: {step['instruction']}",
                    )
                ]
            }
        )
        evidence.append({"step": step, "result": result})

    return {**state, "evidence": evidence}

def answer_node(state: PipelineState) -> PipelineState:
    answer = llm.invoke(
        f"Role: {state['role']}. Question: {state['question']}. Evidence: {state['evidence']}. "
        f"Write a grounded answer."
    )
    return {**state, "answer": answer.content}


def confidence_node(state: PipelineState) -> PipelineState:
    response = router.invoke(
        {
            "state": {
                "user role": state["role"],
                "user question": state["question"],
                "planned steps": state["plan"],
                "found evidence": state["evidence"],
                "answer": state.get("answer", ""),
            },
            "question": {
                "pass": Noul(
                    instructions=(
                        "Estimate whether the current evidence and answer are sufficient "
                        "to answer the user's question accurately and for their role."
                    ),
                    criteria={
                        "true": {
                            "role_fit": "The response addresses the user's role: an engineer investigates errors, a team lead identifies patterns, and a manager prioritises actions.",
                            "evidence": "The evidence is relevant, specific, and sufficient to support the answer.",
                            "answer": "The answer is grounded in the evidence and explains its conclusions reliably.",
                        },
                        "false": {
                            "role_fit": "The response does not address what the user's role needs to know.",
                            "evidence": "The evidence is missing, irrelevant, contradictory, or insufficient.",
                            "answer": "The answer is missing, unsupported by the evidence, or unreliable.",
                        }
                    }
                ),

                "next_state": Choice(
                    instructions=(
                        "Choose the next pipeline state using the current evidence and answer. "
                        "Select plan when more or better evidence is needed. Select answer "
                        "when the evidence is sufficient but the answer must be generated or revised. "
                        "Select conclude only when the answer is supported and ready to return."
                    ),
                    criteria={
                        "plan": "Gather more evidence or revise the plan because the current evidence does not sufficiently answer the question.",
                        "answer": "Use the current evidence to generate or revise an answer that is grounded and appropriate for the user's role.",
                        "conclude": "The evidence and answer are sufficient, grounded, and ready to return to the user.",
                    },
                ),
                "revision_reason": Choice(
                    instructions=(
                        "Identify the main reason the current attempt should be revised. "
                        "Choose none only when the evidence and answer are ready to return."
                    ),
                    criteria={
                        "missing_evidence": "The plan did not collect enough evidence to answer the question.",
                        "irrelevant_evidence": "The collected evidence does not address the question or is contradictory.",
                        "unsupported_answer": "The evidence is useful, but the answer contains unsupported claims or omissions.",
                        "role_mismatch": "The evidence and answer do not address the user's role or decision needs.",
                        "none": "The evidence is relevant and sufficient, and the answer is grounded and ready to return.",
                    },
                ),
            }
        }
    )

    route_answer = response.choices["next_state"]
    next_state = max(route_answer.probabilities, key=route_answer.probabilities.get)
    reason_answer = response.choices["revision_reason"]
    revision_reason = max(reason_answer.probabilities, key=reason_answer.probabilities.get)

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
        f"next_state={next_state} revision_reason={revision_reason}"
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


def route_after_confidence(state: PipelineState) -> Literal["plan", "answer", "conclude"]:
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