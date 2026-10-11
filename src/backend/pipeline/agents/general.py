from langchain.agents import create_agent

from backend.pipeline.models import groq_gpt_oss_20b


general_agent = create_agent(
    model=groq_gpt_oss_20b,
)
