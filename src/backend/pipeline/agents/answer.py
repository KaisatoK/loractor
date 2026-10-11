from langchain.agents import create_agent

from backend.pipeline.model import groq_gpt_oss_120b
from backend.pipeline.system_prompts import ANSWER_SYSTEM_PROMPT
from backend.pipeline.utils import make_error_middleware

answer_agent = create_agent(
    model=groq_gpt_oss_120b,
    middleware=[make_error_middleware()],
    system_prompt=ANSWER_SYSTEM_PROMPT,
)
