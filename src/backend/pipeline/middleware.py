"""Agent middleware for model/tool error handling."""

import logging
import os
import re
import time
from collections.abc import Callable

from langchain.agents.middleware import (
    ModelResponse,
    wrap_model_call,
    wrap_tool_call,
)
from langchain.messages import AIMessage, ToolMessage
from langchain.tools.tool_node import ToolCallRequest
from langgraph.types import Command

logger = logging.getLogger(__name__)

MAX_MODEL_RETRIES = int(os.getenv("MODEL_MAX_RETRIES", "3"))
RETRY_BASE_DELAY = float(os.getenv("MODEL_RETRY_BASE_DELAY", "1.0"))
RETRY_MAX_DELAY = float(os.getenv("MODEL_RETRY_MAX_DELAY", "20.0"))
# Transient provider failures worth retrying with backoff.
RETRYABLE_KINDS = {"rate_limit", "connection"}

USER_MESSAGES = {
    "rate_limit": "The model is rate-limited right now. Please try again in a few minutes.",
    "too_large": "The request is too large for the model. Please shorten it.",
    "connection": "Could not reach the model. Please try again.",
    "unknown": f"The model failed to process the request after {MAX_MODEL_RETRIES} attempts. Please try again.",
}

_RETRY_AFTER_RE = re.compile(
    r"(?:retry[- ]?after|try again in)\D{0,12}?(\d+(?:\.\d+)?)\s*s", re.IGNORECASE
)


def _classify(err: Exception) -> str:
    s = str(err).lower()
    if "rate limit" in s:
        return "rate_limit"
    if "request too large" in s:
        return "too_large"
    if "connection" in s or "timed out" in s:
        return "connection"
    return "unknown"


def _retry_after_seconds(err: Exception) -> float | None:
    """Best-effort parse of a provider-suggested retry delay (e.g. 'retry after 3s')."""
    match = _RETRY_AFTER_RE.search(str(err))
    return float(match.group(1)) if match else None


def make_error_middleware(fallback_factory=None):
    @wrap_model_call
    def middleware(request, handler):
        for attempt in range(MAX_MODEL_RETRIES):
            try:
                return handler(request)
            except Exception as e:
                kind = _classify(e)
                is_last = attempt == MAX_MODEL_RETRIES - 1
                if kind in RETRYABLE_KINDS and not is_last:
                    delay = min(RETRY_BASE_DELAY * (2**attempt), RETRY_MAX_DELAY)
                    suggested = _retry_after_seconds(e)
                    if suggested is not None:
                        delay = max(delay, suggested)
                    logger.warning(
                        "Model call failed (%s), retrying in %.1fs (attempt %s/%s)",
                        kind,
                        delay,
                        attempt + 1,
                        MAX_MODEL_RETRIES,
                    )
                    time.sleep(delay)
                    continue

                msg = USER_MESSAGES[kind] + f" (Error: {e})"
                return ModelResponse(
                    result=[AIMessage(content=msg)],
                    structured_response=(
                        fallback_factory(msg) if fallback_factory else None
                    ),
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
