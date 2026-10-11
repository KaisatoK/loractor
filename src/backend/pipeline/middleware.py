"""Agent middleware for model/tool error handling."""

import logging
from collections.abc import Callable

from langchain.agents.middleware import (
    ModelRequest,
    ModelResponse,
    wrap_model_call,
    wrap_tool_call,
)
from langchain.messages import AIMessage, ToolMessage
from langchain.tools.tool_node import ToolCallRequest
from langgraph.types import Command

logger = logging.getLogger(__name__)

MAX_MODEL_RETRIES = 3

USER_MESSAGES = {
    "rate_limit": "The model is rate-limited right now. Please try again in a few minutes.",
    "too_large": "The request is too large for the model. Please shorten it.",
    "connection": "Could not reach the model. Please try again.",
    "unknown": f"The model failed to process the request after {MAX_MODEL_RETRIES} attempts. Please try again.",
}


def _classify(err: Exception) -> str:
    s = str(err).lower()
    if "rate limit" in s:
        return "rate_limit"
    if "request too large" in s:
        return "too_large"
    if "connection" in s or "timed out" in s:
        return "connection"
    return "unknown"


def make_error_middleware(fallback_factory=None):
    @wrap_model_call
    def middleware(request, handler):
        try:
            return handler(request)
        except Exception as e:
            kind = _classify(e)
            msg = USER_MESSAGES[kind] + f" (Error: {e})"
            return ModelResponse(
                result=[AIMessage(content=msg)],
                structured_response=fallback_factory(msg) if fallback_factory else None,
            )

    return middleware


@wrap_tool_call
def log_tool_calls(
    request: ToolCallRequest,
    handler: Callable[[ToolCallRequest], ToolMessage | Command],
) -> ToolMessage | Command:
    """Log every tool call and its arguments for terminal observability."""
    tool_call = request.tool_call
    logger.info("tool call: %s args=%s", tool_call["name"], tool_call.get("args"))
    return handler(request)


@wrap_tool_call
def handle_tool_errors(
    request: ToolCallRequest,
    handler: Callable[[ToolCallRequest], ToolMessage | Command],
) -> ToolMessage | Command:
    """Convert tool exceptions into ToolMessages the model can handle."""
    try:
        return handler(request)
    except Exception as e:
        logger.warning("Tool call %s failed: %s", request.tool_call["name"], e)
        return ToolMessage(
            status="error",
            content=(
                f"The tool could not complete the database operation: {e}"
                " Do not infer or fabricate a result."
            ),
            tool_call_id=request.tool_call["id"],
        )
