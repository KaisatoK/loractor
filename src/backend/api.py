from uuid import UUID

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse

from typing import AsyncIterable, TypedDict

# from backend.graph import chatbot


class ChatRequest(TypedDict):
    user_id: UUID
    message: str


class ChatResponse(TypedDict):
    type: str
    content: str


app = FastAPI()

@app.post("/chat/stream")
async def chat_stream(req: ChatRequest) -> AsyncIterable[ChatResponse]:
    run = chatbot.stream_events(
        {
            "messages": [{"role": "user", "content": req.message}],
        },
        config={
            "configurable": {
                "thread_id": str(req.user_id),
            }
        },
        version="v3",
    )
    
    for name, item in run.interleave("messages", "subgraphs"):
        if name == "messages":
            for token in item.text:
                yield {"type": "messages", "content": token}
        elif name == "subgraphs":
            yield {"type": "subgraphs", "content": item.path}

@app.post("/chat/test")
async def chat_test(req: ChatRequest) -> AsyncIterable[ChatResponse]:
    yield ChatResponse({"type": "subgraphs", "content": "normalize"})
    yield ChatResponse({"type": "messages", "content": "Hello, how "})
    yield ChatResponse({"type": "subgraphs", "content": "retrieval"})
    yield ChatResponse({"type": "subgraphs", "content": "search"})
    yield ChatResponse({"type": "messages", "content": "can I help you?"})
