from datetime import datetime
from uuid import UUID

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse

from pydantic import BaseModel

from langchain.agents import create_agent
from langchain_core.tools import tool
from langchain_ollama import ChatOllama
from langgraph.checkpoint.memory import InMemorySaver

CHAT_MODEL = "qwen3.5:4b"

SYSTEM_PROMPT = (
    "You are a helpful assistant with access to tools for getting the current time "
    "and counting words in text. "
    "Use tools when needed. If the question does not need a tool, answer directly."
)


class ChatRequest(BaseModel):
    user_id: UUID
    message: str


@tool
def current_time() -> str:
    """Return the current local date and time."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@tool
def word_count(text: str) -> int:
    """Count the number of words in a piece of text."""
    return len(text.split())


checkpointer = InMemorySaver()


def build_agent():
    model = ChatOllama(model=CHAT_MODEL, temperature=0)
    return create_agent(
        model=model,
        tools=[current_time, word_count],
        system_prompt=SYSTEM_PROMPT,
        checkpointer=checkpointer,
    )


agent = build_agent()

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

        for message in run.messages:
            for token in message.text:
                yield token

    return StreamingResponse(generate(), media_type="text/event-stream")
