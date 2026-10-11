import json
import logging
from collections.abc import AsyncIterable
from typing import Literal, TypedDict, cast
from uuid import UUID

from fastapi import FastAPI
from langgraph.stream.transformers import CustomTransformer

from backend.pipeline.agents import answer_agent
from backend.pipeline.graph import chatbot, prompts
from backend.pipeline.roles import ROLE_OBJECTIVES
from backend.pipeline.state import PipelineState, RoleName

logger = logging.getLogger(__name__)


class ChatMessage(TypedDict):
    role: str
    content: str


class ChatRequest(TypedDict):
    user_id: UUID
    chat_id: str
    role: RoleName
    effort: Literal["Low", "Medium", "High", "Extreme", "Brutal"]
    history: list[ChatMessage]


class ChatResponse(TypedDict):
    type: str
    content: str


class ReframeRequest(TypedDict):
    target_role: RoleName
    effort: Literal["Low", "Medium", "High", "Extreme", "Brutal"]
    question: str
    previous_answer: str
    tool_results: list


app = FastAPI()


def _render_conversation(history: list[ChatMessage]) -> str:
    """Flatten the chat history into the role-prefixed transcript the agents read."""
    return "\n".join(f"{item['role']}: {item['content']}" for item in history)


def _latest(history: list[ChatMessage], role: str) -> str:
    return next(
        (item["content"] for item in reversed(history) if item["role"] == role), ""
    )


def _thread_id(user_id: UUID, chat_id: str) -> str:
    """Stable per-chat LangGraph thread key, built from the UI's chat id."""
    return f"{user_id}:{chat_id}"


@app.post("/chat/stream")
async def chat_stream(req: ChatRequest) -> AsyncIterable[ChatResponse]:
    history = req["history"]

    initial_state = cast(
        PipelineState,
        {
            "conversation": _render_conversation(history),
            "latest_message": _latest(history, "user"),
            "latest_answer": _latest(history, "assistant"),
            "role": {
                "name": req["role"],
                "objective": ROLE_OBJECTIVES[req["role"]],
            },
            "effort": req["effort"],
            "answer": "",
        },
    )
    run = chatbot.stream_events(
        initial_state,
        config={
            "configurable": {
                "thread_id": _thread_id(req["user_id"], req["chat_id"]),
            }
        },
        version="v3",
        # `custom` is not registered by default; the class is a scope-aware
        # factory, so it is cloned into every subgraph mini-mux. Tool progress
        # events therefore surface on each subgraph handle's `.custom`.
        transformers=[CustomTransformer],
    )

    last_answer = None
    last_tool_results_json = None
    for name, item in run.interleave("messages", "values", "subgraphs", "custom"):
        if name == "messages":
            logger.debug("stream message: %s", item[-1].text)
        elif name == "values":
            # Change detection (not a once-per-request flag): a follow-up turn
            # re-emits the *previous* turn's persisted payloads in its first
            # values event, then the fresh payload once find_evidence completes.
            # Sending on every change means the UI's final payload is always the
            # current turn's; the transient stale one is replaced before render.
            tool_results = item.get("tool_results")
            if tool_results:
                payload = json.dumps(tool_results)
                if payload != last_tool_results_json:
                    yield {"type": "tool_results", "content": payload}
                    last_tool_results_json = payload
            answer = item.get("answer")
            if answer and answer != last_answer:
                yield {"type": "messages", "content": answer}
                last_answer = answer
            if "fail_reason" in item:
                logger.debug("pipeline fail_reason: %s", item["fail_reason"])
        elif name == "subgraphs":
            yield {"type": "subgraph_name", "content": str(item.graph_name)}
            for sub_name, sub_item in item.interleave("messages", "custom"):
                if sub_name == "messages":
                    yield {"type": "subgraph_message", "content": str(sub_item.reasoning)}
                elif isinstance(sub_item, dict) and sub_item.get("type") == "tool_progress":
                    yield {"type": "tool_progress", "content": json.dumps(sub_item)}
        elif name == "custom":
            # No root-scope custom producers today, but forward the same shape
            # so a future node-level writer is surfaced without UI changes.
            if isinstance(item, dict) and item.get("type") == "tool_progress":
                yield {"type": "tool_progress", "content": json.dumps(item)}


@app.post("/chat/test")
async def chat_test(req: ChatRequest) -> AsyncIterable[ChatResponse]:
    yield {"type": "subgraphs", "content": "normalize"}
    yield {"type": "messages", "content": "Hello, how "}
    yield {"type": "subgraphs", "content": "retrieval"}
    yield {"type": "subgraphs", "content": "search"}
    yield {"type": "messages", "content": "can I help you?"}


@app.post("/chat/reframe")
async def chat_reframe(req: ReframeRequest) -> dict:
    """Re-explain an already-completed investigation for another role.

    Stateless one-shot: builds a synthetic prompt state from the supplied
    evidence and invokes the answer agent once. No graph, no tools, no thread
    or checkpointer involvement.
    """
    state = cast(
        PipelineState,
        {
            "role": {
                "name": req["target_role"],
                "objective": ROLE_OBJECTIVES[req["target_role"]],
            },
            "effort": req["effort"],
            "normalized": req["question"],
            "evidence_report": {},
            "tool_results": req["tool_results"],
            "revision_feedback": "No revision is required.",
            "latest_answer": req["previous_answer"],
        },
    )
    result = answer_agent.invoke(
        {"messages": [{"role": "user", "content": prompts.reframe(state)}]}
    )
    return {"answer": result["messages"][-1].text}
