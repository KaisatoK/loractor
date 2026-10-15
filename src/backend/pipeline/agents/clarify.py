from langchain.agents import create_agent

from backend.pipeline.models import deepseek
from backend.pipeline.prompts import CLARIFY_SYSTEM_PROMPT

clarify_agent = create_agent(
    model=deepseek,
    system_prompt=CLARIFY_SYSTEM_PROMPT,
)
