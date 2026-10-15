from langchain.agents import create_agent

from backend.pipeline.models import deepseek
from backend.pipeline.prompts import GENERAL_SYSTEM_PROMPT

general_agent = create_agent(
    model=deepseek,
    system_prompt=GENERAL_SYSTEM_PROMPT,
)
