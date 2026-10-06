from uuid import UUID

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse

from typing import AsyncIterable, TypedDict, cast
from backend.pipeline.graph import chatbot
from backend.pipeline.schemas import PipelineState


class ChatRequest(TypedDict):
    user_id: UUID
    message: str


class ChatResponse(TypedDict):
    type: str
    content: str


app = FastAPI()

@app.post("/chat/stream")
async def chat_stream(req: ChatRequest) -> AsyncIterable[ChatResponse]:
    initial_state = cast(
        PipelineState,
        {
            "question": req["message"],
            "role": {
                "name": "manager",
                "objective": "Provide a concise and accurate answer to the user's question.",
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
                yield {"type": "subgraphs", "content": str(item.path)}

                
@app.post("/chat/test")
async def chat_test(req: ChatRequest) -> AsyncIterable[ChatResponse]:
    yield {"type": "subgraphs", "content": "normalize"}
    yield {"type": "messages", "content": "Hello, how "}
    yield {"type": "subgraphs", "content": "retrieval"}
    yield {"type": "subgraphs", "content": "search"}
    yield {"type": "messages", "content": "can I help you?"}
