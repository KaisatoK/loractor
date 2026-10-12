from langchain.agents import create_agent

from backend.pipeline.models import groq_gpt_oss_20b
from backend.pipeline.prompts import CLARIFY_SYSTEM_PROMPT

clarify_agent = create_agent(
    model=groq_gpt_oss_20b,
    system_prompt=CLARIFY_SYSTEM_PROMPT,
)
