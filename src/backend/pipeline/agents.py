import logging
from collections.abc import Callable
from typing import Any, Literal

from langgraph.types import Command
from langchain.agents import create_agent
from langchain.agents.middleware import ToolCallLimitMiddleware, wrap_tool_call
from langchain.messages import ToolMessage
from langchain.tools import ToolRuntime, tool
from langchain.tools.tool_node import ToolCallRequest
from langchain_groq import ChatGroq
from langchain_ollama import ChatOllama

from backend.db import (
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
from backend.pipeline.utils import load_tool_schema

logger = logging.getLogger(__name__)

model = ChatGroq(model="openai/gpt-oss-20b")
general_model = ChatGroq(model="openai/gpt-oss-20b")
# general_model = ChatOllama(model="qwen3.5:4b")

TABLE_NAME = "log"
TableName = Literal["log"]
LogCategory = Literal["INFO", "WARNING", "ERROR"]


def execute_database_tool(
    *,
    runtime: ToolRuntime,
    operation: str,
    function: Callable[[], Any],
) -> dict:
    writer = runtime.stream_writer
    writer(
        {
            "type": "tool_progress",
            "operation": operation,
            "status": "started",
            "message": f"Starting {operation}.",
        }
    )
    print("#########################################")
    print(f"Executing database tool: {operation}")
    print(f"Tool runtime: {runtime}")
    print(f"Tool function: {function}")
    print("#########################################")

    try:
        data = function()
    except Exception as e:
        logger.exception("Database tool failed: %s", operation)
        writer(
            {
                "type": "tool_progress",
                "operation": operation,
                "status": "failed",
                "message": f"{operation} failed.",
            }
        )
        raise RuntimeError(f"{operation} failed") from e

    writer(
        {
            "type": "tool_progress",
            "operation": operation,
            "status": "completed",
            "message": f"Completed {operation}.",
        }
    )
    return {"status": "success", "operation": operation, "data": data}


@wrap_tool_call
def handle_tool_errors(
    request: ToolCallRequest,
    handler: Callable[[ToolCallRequest], ToolMessage | Command],
) -> ToolMessage | Command:
    """Convert tool exceptions into ToolMessages the model can handle."""
    try:
        return handler(request)
    except Exception as e:
        print(f"Tool call failed: {e}")
        return ToolMessage(
            content=(
                f"The tool could not complete the database operation: {e}"
                " Do not infer or fabricate a result."
            ),
            tool_call_id=request.tool_call["id"],
        )


@tool(args_schema=load_tool_schema("count_by_category"))
def count_by_category_tool(
    start_time: str,
    end_time: str,
    runtime: ToolRuntime,
    table_name: str = TABLE_NAME,
    category: str | None = None,
) -> dict:
    """Count log rows by exact severity within an inclusive time window."""
    print("#########################################")
    print("Executing count_by_category tool")
    print(f"table_name: {table_name}")
    print(f"start_time: {start_time}")
    print(f"end_time: {end_time}")
    print(f"category: {category}")
    print(f"runtime: {runtime}")
    print("#########################################")

    return execute_database_tool(
        runtime=runtime,
        operation="count logs by severity",
        function=lambda: count_by_category(start_time, end_time, table_name, category),
    )


@tool(args_schema=load_tool_schema("get_trend"))
def get_trend_tool(
    start_time: str,
    end_time: str,
    pid: int,
    runtime: ToolRuntime,
    table_name: str = TABLE_NAME,
    category: str | None = None,
) -> dict:
    """Count one known PID by minute; optionally restrict results to one severity."""
    print("#########################################")
    print("Executing get_trend tool")
    print(f"table_name: {table_name}")
    print(f"start_time: {start_time}")
    print(f"end_time: {end_time}")
    print(f"pid: {pid}")
    print(f"category: {category}")
    print(f"runtime: {runtime}")
    print("#########################################")

    return execute_database_tool(
        runtime=runtime,
        operation="get PID log trend",
        function=lambda: get_trend_by_category(
            start_time, end_time, pid, table_name, category
        ),
    )


@tool(args_schema=load_tool_schema("search_logs"))
def search_logs_tool(
    query_text: str,
    runtime: ToolRuntime,
    category: str | None = None,
    table_name: str = TABLE_NAME,
    limit: int = 10,
) -> dict:
    """Search message content; put severity in category, not query_text."""
    print("#########################################")
    print("Executing search_logs tool")
    print(f"table_name: {table_name}")
    print(f"query_text: {query_text}")
    print(f"category: {category}")
    print(f"runtime: {runtime}")
    print("#########################################")

    return execute_database_tool(
        runtime=runtime,
        operation="search log messages",
        function=lambda: search_logs(query_text, table_name, category, limit),
    )


@tool(args_schema=load_tool_schema("top_in_category_by_service"))
def top_in_category_by_service_tool(
    start_time: str,
    end_time: str,
    runtime: ToolRuntime,
    table_name: str = TABLE_NAME,
    category: str | None = None,
    limit: int = 5,
) -> dict:
    """Rank components by log volume, optionally restricted to one severity."""
    return execute_database_tool(
        runtime=runtime,
        operation="rank services by log volume",
        function=lambda: top_in_category_by_service(
            start_time, end_time, table_name, category, limit
        ),
    )


@tool(args_schema=load_tool_schema("compare_periods"))
def compare_periods_tool(
    baseline_start: str,
    baseline_end: str,
    recent_start: str,
    recent_end: str,
    runtime: ToolRuntime,
    table_name: str = TABLE_NAME,
    limit: int = 5,
) -> dict:
    """Compare component/severity counts between baseline and recent windows."""
    print("#########################################")
    print("Executing compare_periods tool")
    print(f"table_name: {table_name}")
    print(f"baseline_start: {baseline_start}")
    print(f"baseline_end: {baseline_end}")
    print(f"recent_start: {recent_start}")
    print(f"recent_end: {recent_end}")
    print(f"runtime: {runtime}")
    print("#########################################")
    return execute_database_tool(
        runtime=runtime,
        operation="compare log periods",
        function=lambda: compare_periods(
            baseline_start,
            baseline_end,
            recent_start,
            recent_end,
            table_name,
            limit,
        ),
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
    middleware=[
        handle_tool_errors,
        ToolCallLimitMiddleware(run_limit=6),
    ],
    system_prompt=(
        "You are an evidence collection agent. Use tools to evaluate every evidence "
        "requirement. Report only facts returned by successful tool calls. A tool error "
        "means the evidence is unavailable, not not_found. Use not_found only when a "
        "successful tool call returned no matching facts. Never guess tool arguments "
        "such as PID or timestamps. Mark a hypothesis supported only when matching "
        "evidence supports it, contradicted only when evidence directly conflicts with "
        "it, otherwise unresolved."
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
