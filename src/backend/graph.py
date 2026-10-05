from langgraph.graph import StateGraph, END
from langgraph.graph.message import add_messages

from langchain_ollama import ChatOllama

from typing import TypedDict, Annotated, Literal

from backend.agents import 

llm = ChatAnthropic(model="claude-sonnet-4-6")

class NormalizedOutput(TypeDict):
    intent: Literal["count", "trend", "compare"]
    explanation: str

class PipelineState(TypedDict):
    question: str
    role: str
    normalized: NormalizedOutput        # output of normalization stage
    evidence: list                      # accumulated results from subagents
    confidence: dict
    answer: str
    
def normalize_node(state: PipelineState) -> PipelineState:
    response = llm.with_structured_output(QueryPlan).invoke(
        f"Role: {state['role']}. Question: {state['question']}. "
        f"Classify intent and extract parameters as structured JSON."
    )
    return {**state, "normalized": response}
    
