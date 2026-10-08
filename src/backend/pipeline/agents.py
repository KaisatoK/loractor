from collections.abc import Callable

from langgraph.types import Command
from langchain.agents import create_agent
from langchain.agents.middleware import ToolCallLimitMiddleware, wrap_tool_call
from langchain.messages import ToolMessage
from langchain.tools.tool_node import ToolCallRequest
from langchain_groq import ChatGroq

from backend.pipeline.tools import *
from backend.pipeline.schemas import (
    EvidenceReport,
    ExecutionPlan,
    NormalizedOutput,
)

model = ChatGroq(model="openai/gpt-oss-20b")
general_model = ChatGroq(model="openai/gpt-oss-20b")
# general_model = ChatOllama(model="qwen3.5:4b")


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
        count_pid_by_category_tool,
        count_service_by_category_tool,
        count_event_id_by_category_tool,
        get_pids_tool,
        get_services_tool,
        get_event_ids_tool,
        get_pid_trend_tool,
        get_service_trend_tool,
        get_event_id_trend_tool,
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
