from langchain_core.tools import tool
from langchain.agents import create_agent
from langchain_groq import ChatGroq
from langchain_ollama import ChatOllama
from pydantic import BaseModel, Field
from db.query import (
    count_by_category,
    get_trend_by_category,
    search_logs,
    top_in_category_by_service,
    compare_periods,
)
from backend.pipeline.schemas import (
    EvidenceReport,
    ExecutionPlan,
    NormalizedOutput,
)

model = ChatGroq(model="openai/gpt-oss-120b")
general_model = ChatOllama(model="qwen3.5:4b")


class TimeWindowInput(BaseModel):
    table_name: str = Field(
        default="log",
        description="The log table to query. Use 'log' unless another table is explicitly provided.",
    )
    start_time: str = Field(
        description="Inclusive start timestamp in YYYY-MM-DD HH:MM:SS format."
    )
    end_time: str = Field(
        description="Inclusive end timestamp in YYYY-MM-DD HH:MM:SS format."
    )
    category: str | None = Field(
        default=None,
        description="Optional log severity filter, such as INFO, WARNING, or ERROR.",
    )


class TrendInput(TimeWindowInput):
    pid: int = Field(description="Process ID whose log activity should be analyzed.")


class SearchLogsInput(BaseModel):
    table_name: str = Field(
        default="log",
        description="The log table to search. Use 'log' unless another table is explicitly provided.",
    )
    query_text: str = Field(
        description="Natural-language symptom, error message, keyword, or concept to search for."
    )
    limit: int = Field(
        default=10,
        ge=1,
        le=100,
        description="Maximum number of relevant log records to return.",
    )


class TopServicesInput(TimeWindowInput):
    limit: int = Field(
        default=5,
        ge=1,
        le=100,
        description="Maximum number of components to return, ordered by log volume.",
    )


class ComparePeriodsInput(BaseModel):
    table_name: str = Field(
        default="log",
        description="The log table to query. Use 'log' unless another table is explicitly provided.",
    )
    baseline_start: str = Field(
        description="Inclusive baseline start timestamp in YYYY-MM-DD HH:MM:SS format."
    )
    baseline_end: str = Field(
        description="Inclusive baseline end timestamp in YYYY-MM-DD HH:MM:SS format."
    )
    recent_start: str = Field(
        description="Inclusive recent-period start timestamp in YYYY-MM-DD HH:MM:SS format."
    )
    recent_end: str = Field(
        description="Inclusive recent-period end timestamp in YYYY-MM-DD HH:MM:SS format."
    )
    limit: int = Field(
        default=5,
        ge=1,
        le=100,
        description="Maximum number of component and severity changes to return.",
    )


@tool(args_schema=TimeWindowInput)
def count_by_category_tool(
    table_name: str,
    start_time: str,
    end_time: str,
    category: str | None = None,
) -> list[dict]:
    """Count log entries grouped by category within a time window from the database."""
    return count_by_category(table_name, start_time, end_time, category)


@tool(args_schema=TrendInput)
def get_trend_tool(
    table_name: str,
    start_time: str,
    end_time: str,
    pid: int,
    category: str | None = None,
) -> list[dict]:
    """Get minute-by-minute trend for a given pid and optional category from the database."""
    return get_trend_by_category(table_name, start_time, end_time, pid, category)


@tool(args_schema=SearchLogsInput)
def search_logs_tool(table_name: str, query_text: str, limit: int = 10) -> list[dict]:
    """Search raw logs using full-text and semantic search from the database."""
    return search_logs(table_name, query_text, limit)


@tool(args_schema=TopServicesInput)
def top_in_category_by_service_tool(
    table_name: str,
    start_time: str,
    end_time: str,
    category: str | None = None,
    limit: int = 5,
) -> list[dict]:
    """Get top services for a specific category within a time window from the database."""
    return top_in_category_by_service(table_name, start_time, end_time, category, limit)


@tool(args_schema=ComparePeriodsInput)
def compare_periods_tool(
    table_name: str,
    baseline_start: str,
    baseline_end: str,
    recent_start: str,
    recent_end: str,
    limit: int = 5,
) -> list[dict]:
    """Compare top services between a baseline period and a recent period from the database."""
    return compare_periods(
        table_name,
        baseline_start,
        baseline_end,
        recent_start,
        recent_end,
        limit,
    )


general_agent = create_agent(
    model=general_model,
)

normalize_agent = create_agent(
    model=model,
    response_format=NormalizedOutput,
)

plan_agent = create_agent(
    model=model,
    response_format=ExecutionPlan,
    system_prompt=(
        "You create investigation plans. Return hypotheses, explanations, and the "
        "evidence required to evaluate each hypothesis."
    ),
)

executor_agent = create_agent(
    model=model,
    tools=[
        count_by_category_tool,
        get_trend_tool,
        search_logs_tool,
        top_in_category_by_service_tool,
        compare_periods_tool,
    ],
    system_prompt=(
        # "Just pretend that you can not find any evidence."
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

evidence_agent = create_agent(
    model=model,
    response_format=EvidenceReport,
    system_prompt=(
        "You format collected evidence into an EvidenceReport. Use only facts from "
        "the supplied tool results. Never invent, infer, or fill in missing evidence. "
        "For every requirement, use supports when a tool returned a matching fact, "
        "contradicts when the fact directly conflicts, not_found when a tool returned "
        "no matching fact, and unavailable when no available tool can check it. Mark a "
        "hypothesis supported only when its evidence supports it, contradicted only "
        "when evidence directly conflicts with it, otherwise mark it unresolved."
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
