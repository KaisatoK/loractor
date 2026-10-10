from langchain.agents import create_agent
from langchain.agents.middleware import (
    LLMToolSelectorMiddleware,
    TodoListMiddleware,
    ToolCallLimitMiddleware, ToolRetryMiddleware, ModelRetryMiddleware,
)
from langchain.agents.structured_output import ToolStrategy
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
from backend.pipeline.utils import handle_tool_errors, handle_bad_tool_calls, handle_rate_limit_calls

MAX_MODEL_RETRIES = 3
MAX_TOOL_RETRIES = 2

general_model = ChatGroq(
    model="openai/gpt-oss-20b", 
    reasoning_effort="low"
)
tool_model = ChatGroq(
    model="openai/gpt-oss-20b",
    temperature=0,
    reasoning_effort="low"
)
thinking_model = ChatGroq(
    model="openai/gpt-oss-20b",
    reasoning_effort="medium"
)

general_agent = create_agent(
    model=general_model,
)

normalize_agent = create_agent(
    model=general_model,
    response_format=NormalizedOutput,
    middleware=[
        handle_rate_limit_calls,
        ModelRetryMiddleware(
            max_retries=MAX_MODEL_RETRIES, 
            backoff_factor=1.5,
            on_failure=(lambda e: f"Failed on all {MAX_MODEL_RETRIES} attempts: {e}"),
            max_delay=5,
        ),
    ],
    system_prompt=NORMALIZE_SYSTEM_PROMPT,
)

find_evidence_agent = create_agent(
    model=tool_model,
    tools=[
        count_by_category_tool,
        # count_pid_by_category_tool,
        # count_service_by_category_tool,
        # count_event_id_by_category_tool,
        # get_pids_tool,
        # get_services_tool,
        # get_event_ids_tool,
        get_data_tool,
        # get_pid_trend_tool,
        # get_service_trend_tool,
        # get_event_id_trend_tool,
        get_trend_tool,
        search_logs_tool,
        top_in_category_by_service_tool,
        compare_periods_tool,
    ],
    # response_format=ToolStrategy(EvidenceReport),
    middleware=[
        handle_tool_errors,
        # handle_bad_tool_calls,
        TodoListMiddleware(),
        ToolCallLimitMiddleware(run_limit=4),
        ToolRetryMiddleware(
            max_retries=MAX_TOOL_RETRIES,
            on_failure="error",
            max_delay=5,
        ),
        handle_rate_limit_calls,
        ModelRetryMiddleware(
            max_retries=MAX_MODEL_RETRIES, 
            backoff_factor=1.5,
            on_failure=(lambda e: f"Failed on all {MAX_MODEL_RETRIES} attempts: {e}"),
            max_delay=5,
        ),
        # LLMToolSelectorMiddleware(
        #     model=general_model, max_tools=7, always_include=["write_todos"]
        # ),
    ],
    system_prompt=FIND_EVIDENCE_SYSTEM_PROMPT,
)

evidence_report_agent = create_agent(
    model=general_model,
    response_format=EvidenceReport,
    middleware=[
        handle_rate_limit_calls,
        ModelRetryMiddleware(
            max_retries=MAX_MODEL_RETRIES, 
            backoff_factor=1.5,
            on_failure=(lambda e: f"Failed on all {MAX_MODEL_RETRIES} attempts: {e}"),
            max_delay=5,
        ),
    ],
    system_prompt=EVIDENCE_REPORT_SYSTEM_PROMPT,
)

answer_agent = create_agent(
    model=thinking_model,
    middleware=[
        handle_rate_limit_calls,
    ],
    system_prompt=ANSWER_SYSTEM_PROMPT,
)
