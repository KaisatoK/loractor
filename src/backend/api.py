from uuid import UUID

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse

from typing import TypedDict

class ChatRequest(TypedDict):
    user_id : UUID
    message : str

app = FastAPI()

@app.post("/chat/stream")
async def chat_stream(req: ChatRequest):
    def generate():
        run = agent.stream_events(
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

    return StreamingResponse(generate(), media_type="text/event-stream")