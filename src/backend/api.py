import ast
import hashlib

from collections.abc import AsyncIterable
import json
from typing import Literal, TypedDict, cast
from uuid import UUID
from fastapi import FastAPI
from langgraph.stream.transformers import CustomTransformer

from backend.pipeline.graph import chatbot
from backend.pipeline.schema.schemas import PipelineState


class ChatRequest(TypedDict):
    user_id: UUID
    role: Literal["engineer", "team_lead", "manager"]
    effort: Literal["Low", "Medium", "High", "Extreme", "Brutal"]
    message: str


class ChatResponse(TypedDict):
    type: str
    content: str


ROLE_OBJECTIVES: dict[str, str] = {
    "engineer": (
        "Help the engineer identify the most likely cause of a specific error, inspect "
        "the supporting log evidence, and determine the next technical action required "
        "to resolve or further diagnose the issue. Answer specifically and technically. "
        "Be evidence-first, chronological, and traceable: include timestamps, trace IDs, "
        "request IDs, services, hosts, error codes, and log sources when available. "
        "Separate confirmed facts from likely causes and uncertainties. Structure the "
        "answer as: What happened; Most likely cause; Relevant evidence; Event timeline; "
        "Confidence and uncertainties; Recommended next debugging steps."
    ),
    "team_lead": (
        "Help the team lead understand recurring failures, identify patterns across "
        "services and time periods, assess operational impact, and determine which "
        "engineering improvement would prevent the most repeated incidents. Focus on "
        "aggregated, comparative, quantitative findings: counts, rates, trends, and "
        "affected users or requests when available. Prioritize recurring problems by "
        "frequency, severity, and impact, grouping symptoms only when the evidence "
        "supports it. Structure the answer as: Pattern summary; Top recurring issues; "
        "Trend and comparison; Affected systems or workflows; Likely contributing "
        "factors; Recommended team actions, including ownership and preventive work."
    ),
    "manager": (
        "Help the manager decide what to prioritize by translating technical findings "
        "into business impact, operational risk, urgency, and expected value of corrective "
        "action. Be concise and outcome-focused. Present a short ranked list based on "
        "impact, urgency, recurrence, effort, and confidence. Explain effects on "
        "customers, revenue, availability, productivity, or risk only when supported by "
        "the data. Structure the answer as: Executive summary; Business or operational "
        "impact; Prioritized issues; Reason for the ranking; Recommended decision; "
        "Success measures."
    ),
}

app = FastAPI()


def _parse_history(message: str) -> list[dict[str, str]]:
    """Parse the UI's Python-repr history string into a list of chat items.

    The UI sends an append-only list of ``{"role", "content"}`` dicts rendered
    with ``str(...)``. ``ast.literal_eval`` handles both that repr and the
    double-quoted JSON-style form. On any parse failure the raw string is
    treated as a single user message.
    """
    try:
        parsed = ast.literal_eval(message)
    except (ValueError, SyntaxError):
        return [{"role": "user", "content": message}]

    if not isinstance(parsed, list):
        return [{"role": "user", "content": message}]

    history: list[dict[str, str]] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        content = item.get("content")
        if isinstance(role, str) and isinstance(content, str):
            history.append({"role": role, "content": content})
    return history or [{"role": "user", "content": message}]


def _thread_id(user_id: UUID, role: str, history: list[dict[str, str]]) -> str:
    """Stable per-chat thread key.

    The UI never sends a chat id, but the opening message is append-only and
    therefore constant for the life of a chat. Hashing it, together with the
    user and role, keeps each chat on its own LangGraph thread.

    Known limitation: two chats from the same user with the same role AND an
    identical opening message collide onto one thread. Acceptable for this
    prototype (the UI does not expose a chat id).
    """
    first_user_message = next(
        (item["content"] for item in history if item["role"] == "user"),
        "",
    )
    digest = hashlib.sha1(first_user_message.encode("utf-8")).hexdigest()[:12]
    return f"{user_id}:{role}:{digest}"


@app.post("/chat/stream")
async def chat_stream(req: ChatRequest) -> AsyncIterable[ChatResponse]:
    history = _parse_history(req["message"])
    latest_message = next(
        (item["content"] for item in reversed(history) if item["role"] == "user"),
        req["message"],
    )
    latest_answer = next(
        (item["content"] for item in reversed(history) if item["role"] == "assistant"),
        "",
    )

    initial_state = cast(
        PipelineState,
        {
            "conversation": req["message"],
            "latest_message": latest_message,
            "latest_answer": latest_answer,
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
                "thread_id": _thread_id(req["user_id"], req["role"], history),
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
            print(f"@@@ API check @@@ name={name}: text={item[-1].text}")
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
                print(f"@@@ API check @@@ name={name}: text={item['fail_reason']}")
        elif name == "subgraphs":
            # print(f"@@@ API check @@@ name={name}: graph_name={item.graph_name}")
            yield {"type": "subgraph_name", "content": str(item.graph_name)}
            for sub_name, sub_item in item.interleave("messages", "custom"):
                if sub_name == "messages":
                    # print(f"@@@ API check @@@ name={name}: reasoning={message.reasoning}, text={message.text}")
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
