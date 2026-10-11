from langchain.agents import create_agent
from langchain.agents.middleware import (
    TodoListMiddleware,
    ToolCallLimitMiddleware,
    ToolRetryMiddleware,
)

from backend.pipeline.effort import get_effort_spec
from backend.pipeline.middleware import (
    handle_tool_errors,
    log_tool_calls,
    make_error_middleware,
)
from backend.pipeline.models import deepseek
from backend.pipeline.prompts import FIND_EVIDENCE_SYSTEM_PROMPT
from backend.pipeline.tools import (
    compare_periods_tool,
    count_by_category_tool,
    get_data_tool,
    get_trend_tool,
    search_logs_tool,
    top_in_category_by_service_tool,
)

MAX_TOOL_RETRIES = 2

_find_evidence_agents: dict[str, object] = {}


def get_find_evidence_agent(effort: str):
    """Evidence agent configured for an effort level (one instance per level, cached)."""
    if effort not in _find_evidence_agents:
        spec = get_effort_spec(effort)
        _find_evidence_agents[effort] = create_agent(
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
                log_tool_calls,
                handle_tool_errors,
                TodoListMiddleware(),
                ToolCallLimitMiddleware(run_limit=spec.max_tool_calls),
                ToolRetryMiddleware(
                    max_retries=MAX_TOOL_RETRIES,
                    on_failure="error",
                    max_delay=5,
                ),
                make_error_middleware(),
            ],
            system_prompt=FIND_EVIDENCE_SYSTEM_PROMPT,
        )
    return _find_evidence_agents[effort]
