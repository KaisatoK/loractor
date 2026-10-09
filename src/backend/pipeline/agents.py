from collections.abc import Callable

from langchain.agents import create_agent
from langchain.agents.middleware import ToolCallLimitMiddleware, wrap_tool_call
from langchain.messages import ToolMessage
from langchain.tools.tool_node import ToolCallRequest
from langchain_groq import ChatGroq
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.types import Command
from backend.pipeline.tools import *

from backend.pipeline.tools.schemas import (
    EvidenceReport,
    NormalizeOutput,
)
from backend.pipeline.system_prompts import (
    ANSWER_SYSTEM_PROMPT,
    EVIDENCE_REPORT_SYSTEM_PROMPT,
    FIND_EVIDENCE_SYSTEM_PROMPT,
    NORMALIZE_SYSTEM_PROMPT,
)

general_model = ChatGroq(model="openai/gpt-oss-20b")
thinking_model = ChatGroq(model="openai/gpt-oss-20b")


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
    model=general_model,
    response_format=NormalizeOutput,
    system_prompt=NORMALIZE_SYSTEM_PROMPT,
)

find_evidence_agent = create_agent(
    model=thinking_model,
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
    system_prompt=FIND_EVIDENCE_SYSTEM_PROMPT,
)

evidence_report_agent = create_agent(
    model=thinking_model,
    response_format=EvidenceReport,
    system_prompt=EVIDENCE_REPORT_SYSTEM_PROMPT,
)

answer_agent = create_agent(
    model=thinking_model,
    system_prompt=ANSWER_SYSTEM_PROMPT,
)
