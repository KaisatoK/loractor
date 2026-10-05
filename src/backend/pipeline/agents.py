from langchain_core.tools import tool
from langchain.agents import create_agent
from langchain_ollama import ChatOllama
from src.backend.pipeline.schemas import (
    EvidenceReport,
    ExecutionPlan,
    NormalizedOutput,
)

model = ChatOllama(model="qwen3.5:4b")


@tool
def count_by_category_tool(hours: int, category: str | None = None):
    """Count log entries grouped by category within a time window."""


@tool
def search_logs_tool(query: str, limit: int = 10) -> dict[str, object]:
    """Placeholder for searching raw logs by text."""
    return {
        "status": "not_implemented",
        "tool": "search_logs_tool",
        "message": "Raw log search is not implemented yet.",
        "query": query,
        "limit": limit,
    }


@tool
def get_trend_tool(service: str, days: int = 7) -> dict[str, object]:
    """Placeholder for retrieving a service trend over time."""
    return {
        "status": "not_implemented",
        "tool": "get_trend_tool",
        "message": "Trend retrieval is not implemented yet.",
        "service": service,
        "days": days,
    }


normalize_agent = create_agent(
    model=model,
    response_format=NormalizedOutput,
)

plan_agent = create_agent(
    model=model,
    response_format=ExecutionPlan,
    system_prompt=(
        "You create investigation plans. Return hypotheses, explanations, and the "
        "evidence required to evaluate each hypothesis. Do not name tools or invent "
        "evidence."
    ),
)

executor_agent = create_agent(
    model=model,
    tools=[count_by_category_tool, search_logs_tool, get_trend_tool],
    response_format=EvidenceReport,
    system_prompt=(
        "You are an evidence collection agent. Receive investigation hypotheses and "
        "required evidence, then use the available tools to collect it. You may only "
        "report facts returned by tool calls. Never invent, infer, or fill in missing "
        "evidence. For every requirement, use supports when a tool returned a matching "
        "fact that supports the hypothesis, use contradicts when the fact directly "
        "conflicts with the hypothesis, "
        "contradicts when it directly conflicts with the hypothesis, not_found when a "
        "tool ran but returned no matching fact, and unavailable when no available tool "
        "can check it. Mark a hypothesis supported only when its evidence supports it, "
        "contradicted only when evidence directly conflicts with it, otherwise mark it "
        "unresolved."
    ),
)

answer_agent = create_agent(
    model=model,
    system_prompt=(
        "You write the final answer from the normalized request, investigation "
        "hypotheses, evidence statuses, and observed tool results. Do not claim that "
        "a hypothesis is proven unless the evidence supports it. Clearly distinguish "
        "collected, not found, and unavailable evidence."
    ),
)

