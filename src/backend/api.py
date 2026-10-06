from uuid import UUID

from fastapi import FastAPI

from typing import AsyncIterable, TypedDict, Literal, cast
from backend.pipeline.graph import chatbot
from backend.pipeline.schemas import PipelineState


class ChatRequest(TypedDict):
    user_id: UUID
    role: Literal["engineer", "team_lead", "manager"]
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


@app.post("/chat/stream")
async def chat_stream(req: ChatRequest) -> AsyncIterable[ChatResponse]:
    initial_state = cast(
        PipelineState,
        {
            "question": req["message"],
            "role": {
                "name": req["role"],
                "objective": ROLE_OBJECTIVES[req["role"]],
            },
        },
    )
    run = chatbot.stream_events(
        initial_state,
        config={
            "configurable": {
                "thread_id": str(req["user_id"]),
            }
        },
        version="v3",
    )

    last_answer = None
    for name, item in run.interleave("values", "subgraphs"):
        if name == "values":
            answer = item.get("answer")
            if answer and answer != last_answer:
                yield {"type": "messages", "content": answer}
                last_answer = answer
        elif name == "subgraphs":
            yield {"type": "subgraphs", "content": item.path[0]}


@app.post("/chat/test")
async def chat_test(req: ChatRequest) -> AsyncIterable[ChatResponse]:
    yield {"type": "subgraphs", "content": "normalize"}
    yield {"type": "messages", "content": "Hello, how "}
    yield {"type": "subgraphs", "content": "retrieval"}
    yield {"type": "subgraphs", "content": "search"}
    yield {"type": "messages", "content": "can I help you?"}
