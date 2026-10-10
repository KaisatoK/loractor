from langchain.agents import create_agent
from langchain.agents.middleware import (
    LLMToolSelectorMiddleware,
    TodoListMiddleware,
    ToolCallLimitMiddleware,
)
from langchain_groq import ChatGroq
from backend.pipeline.tools import *

from backend.pipeline.schema.schemas import (
    EvidenceReport,
    NormalizedOutput,
)
from backend.pipeline.system_prompts import (
    ANSWER_SYSTEM_PROMPT,
    EVIDENCE_REPORT_SYSTEM_PROMPT,
    FIND_EVIDENCE_SYSTEM_PROMPT,
    NORMALIZE_SYSTEM_PROMPT,
)
from backend.pipeline.utils import handle_tool_errors, handle_bad_tool_calls

general_model = ChatGroq(model="openai/gpt-oss-20b")
thinking_model = ChatGroq(model="openai/gpt-oss-20b")


general_agent = create_agent(
    model=general_model,
)

normalize_agent = create_agent(
    model=general_model,
    response_format=NormalizedOutput,
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
        TodoListMiddleware,
        ToolCallLimitMiddleware(run_limit=6),
        LLMToolSelectorMiddleware(
            model=general_model, max_tools=7, always_include=["write_todos"]
        ),
        handle_bad_tool_calls,
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
