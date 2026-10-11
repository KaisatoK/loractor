import json
import logging
import jsonref

from collections.abc import Callable
from pathlib import Path
from typing import Any

from langchain.tools import ToolRuntime
from langchain.agents.middleware import (
    ModelRequest,
    ModelResponse,
    wrap_model_call,
    wrap_tool_call,
)
from langchain.messages import AIMessage, HumanMessage, ToolMessage
from langchain.tools.tool_node import ToolCallRequest
from langgraph.types import Command

logger = logging.getLogger(__name__)
TABLE_NAME = "log"
MAX_MODEL_RETRIES = 3
MAX_RESULT_ROWS = 50

LOG_SCHEMA_PATH = Path(__file__).resolve().parents[1] / "db" / "log_table_schema.yaml"
LOG_SCHEMA = LOG_SCHEMA_PATH.read_text(encoding="utf-8")
TOOL_SCHEMA_PATH = Path(__file__).resolve().parents[1] / "data" / "tool_schema.json"
TOOL_SCHEMA = jsonref.loads(TOOL_SCHEMA_PATH.read_text(encoding="utf-8"))


@wrap_model_call
def handle_bad_tool_calls(
    request: ModelRequest, handler: Callable[[ModelRequest], ModelResponse]
) -> ModelResponse:
    for attempt in range(MAX_MODEL_RETRIES):
        try:
            return handler(request)
        except Exception as e:
            err = str(e)

            if "rate limit" in err.lower():
                raise

            print(f"[retry {attempt + 1}] {err[:300]}")

            overrides = {}
            if "tool choice is" in err.lower():
                overrides["tool_choice"] = "auto"
            else:
                overrides["messages"] = [
                    *request.messages,
                    HumanMessage(
                        content=(
                            f"Your previous tool call was rejected: {err[:500]}\n"
                            "Call one of the available tools with parameters matching its "
                            "schema, or reply with your final answer in plain text."
                        )
                    ),
                ]

            request = request.override(**overrides)

    return ModelResponse(
        result=[
            AIMessage(
                content="Tool calls kept failing validation; no further evidence could be collected."
            )
        ]
    )


USER_MESSAGES = {
    "rate_limit": "The model is rate-limited right now. Please try again in a few minutes.",
    "too_large": "The request is too large for the model. Please shorten it.",
    "connection": "Could not reach the model. Please try again.",
    "unknown": f"The model failed to process the request after {MAX_MODEL_RETRIES} attempts. Please try again.",
}


def _classify(err: Exception) -> str | None:
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


def model_fail_check(e: Exception) -> str:
    if "rate limit reached" in str(e).lower():
        return "The model is currently rate-limited and cannot process the request. Please try again later."
    elif "request too large for model" in str(e).lower():
        return "The request is too large for the model to process. Please reduce the size of the input and try again."
    elif "connection" in str(e).lower():
        return "The model is currently experiencing connection issues and cannot process the request. Please try again later."
    return f"Failed on all {MAX_MODEL_RETRIES} attempts: {e}"


@wrap_tool_call
def handle_tool_errors(
    request: ToolCallRequest,
    handler: Callable[[ToolCallRequest], ToolMessage | Command],
) -> ToolMessage | Command:
    """Convert tool exceptions into ToolMessages the model can handle."""
    try:
        print(f"@@@ Tool Check: Handling tool call for {request.tool_call['name']}.")
        return handler(request)
    except Exception as e:
        print(f"Tool call failed: {e}")
        return ToolMessage(
            status="error",
            content=(
                f"The tool could not complete the database operation: {e}"
                " Do not infer or fabricate a result."
            ),
            tool_call_id=request.tool_call["id"],
        )


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


def collect_tool_results(messages) -> list[dict]:
    """Extract successful SQL tool results from an agent message list, ready for UI display."""
    calls = {}
    for m in messages:
        for tc in getattr(m, "tool_calls", None) or []:
            calls[tc["id"]] = (tc["name"], tc.get("args", {}))
    records = []
    for m in messages:
        if getattr(m, "type", "") != "tool":
            continue
        try:
            payload = json.loads(m.content) if isinstance(m.content, str) else m.content
        except Exception:
            continue
        if not isinstance(payload, dict) or payload.get("status") != "success":
            continue
        data = payload.get("data") or []
        if not data:
            continue
        name, args = calls.get(m.tool_call_id, (None, None))
        records.append(
            {
                "id": f"R{len(records) + 1}",
                "tool_name": name,
                "arguments": args,
                "row_count": len(data),
                "rows": data[:MAX_RESULT_ROWS],
            }
        )
    return records
