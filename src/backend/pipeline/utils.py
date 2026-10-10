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
from backend.pipeline.tools import *

logger = logging.getLogger(__name__)
TABLE_NAME = "log"
MAX_MODEL_RETRIES = 3

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


@wrap_model_call
def handle_rate_limit_calls(
    request: ModelRequest, handler: Callable[[ModelRequest], ModelResponse]
) -> ModelResponse:
    try:
        print(f"@@@ Model Check: Handling model request with {len(request.messages)} messages. Last message: {request.messages[-1].content[:100]}...")
        return handler(request)
    except Exception as e:
        err = str(e)

        if "rate limit exceeded" in err.lower():
            return ModelResponse(
                result=[
                    AIMessage(
                        content=(
                            "The model is currently rate-limited and cannot process "
                            "the request. Please try again later."
                        )
                    )
                ]
            )
        elif "request too large for model" in err.lower():
            return ModelResponse(
                result=[
                    AIMessage(
                        content=(
                            "The request is too large for the model to process. "
                            "Please reduce the size of the input and try again."
                        )
                    )
                ]
            )
        elif "connection" in err.lower():
            return ModelResponse(
                result=[
                    AIMessage(
                        content=(
                            "The model is currently experiencing connection issues and "
                            "cannot process the request. Please try again later."
                        )
                    )
                ]
            )
        else:
            raise
            


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
