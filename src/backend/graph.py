from langgraph.graph import StateGraph, END
from langgraph.graph.message import add_messages

from langchain_ollama import ChatOllama

from typing import TypedDict, Annotated, Literal

from backend.agents import retrieval_agent, search_agent

llm = ChatOllama(model="qwen3.5:4b")

class NormalizedOutput(TypedDict):
    intent: Literal["count", "trend", "compare", "search", "unknown"]
    explanation: str

class PipelineState(TypedDict):
    question: str
    role: str
    normalized: NormalizedOutput        # output of normalization stage
    evidence: list                      # accumulated results from subagents
    confidence: dict
    answer: str
    
def normalize_node(state: PipelineState) -> PipelineState:
    response = llm.with_structured_output(NormalizedOutput).invoke(
        f"Role: {state['role']}. Question: {state['question']}. "
        f"Classify intent and extract parameters as structured JSON."
    )
    return {**state, "normalized": response}
    
def route_node(state: PipelineState) -> Literal["retrieval", "search"]:
    intent = state["normalized"].get("intent")
    if intent in ("count", "trend", "compare"):
        return "retrieval"
    return "search"

def retrieval_node(state: PipelineState) -> PipelineState:
    result = retrieval_agent.invoke({"messages": [("user", str(state["normalized"]))]})
    return {**state, "evidence": state.get("evidence", []) + [result]}

def search_node(state: PipelineState) -> PipelineState:
    result = search_agent.invoke({"messages": [("user", str(state["normalized"]))]})
    return {**state, "evidence": state.get("evidence", []) + [result]}

def confidence_node(state: PipelineState) -> PipelineState:
    n_results = len(state["evidence"])
    confidence = "low" if n_results == 0 else "high" if n_results >= 2 else "medium"
    return {**state, "confidence": {"level": confidence}}

def conclude_node(state: PipelineState) -> PipelineState:
    answer = llm.invoke(
        f"Role: {state['role']}. Question: {state['question']}. Evidence: {state['evidence']}. "
        f"Confidence: {state['confidence']}. Write a grounded answer. "
        f"If confidence is low, say so explicitly."
    )
    return {**state, "answer": answer.content}


graph = StateGraph(PipelineState)
graph.add_node("normalize", normalize_node)
graph.add_node("retrieval", retrieval_node)
graph.add_node("search", search_node)
graph.add_node("confidence", confidence_node)
graph.add_node("conclude", conclude_node)

graph.set_entry_point("normalize")
graph.add_conditional_edges("normalize", route_node, {"retrieval": "retrieval", "search": "search"})
graph.add_edge("retrieval", "confidence")
graph.add_edge("search", "confidence")
graph.add_edge("confidence", "conclude")
graph.add_edge("conclude", END)

chatbot = graph.compile()