from langchain.agents import create_agent
from langchain.agents.middleware import (
    TodoListMiddleware,
    ToolCallLimitMiddleware,
    ToolRetryMiddleware,
)

from backend.pipeline.model import deepseek
from backend.pipeline.system_prompts import FIND_EVIDENCE_SYSTEM_PROMPT
from backend.pipeline.tools import (
    compare_periods_tool,
    count_by_category_tool,
    get_data_tool,
    get_trend_tool,
    search_logs_tool,
    top_in_category_by_service_tool,
)
from backend.pipeline.utils import handle_tool_errors, make_error_middleware

MAX_TOOL_RETRIES = 2


find_evidence_agent = create_agent(
    model=deepseek,
    tools=[
        count_by_category_tool,
        get_data_tool,
        get_trend_tool,
        search_logs_tool,
        top_in_category_by_service_tool,
        compare_periods_tool,
    ],
    middleware=[
        handle_tool_errors,
        TodoListMiddleware(),
        ToolCallLimitMiddleware(run_limit=6),
        ToolRetryMiddleware(
            max_retries=MAX_TOOL_RETRIES,
            on_failure="error",
            max_delay=5,
        ),
        make_error_middleware(),
    ],
    system_prompt=FIND_EVIDENCE_SYSTEM_PROMPT,
)
