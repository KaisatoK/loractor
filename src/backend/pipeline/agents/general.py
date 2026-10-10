from langchain.agents import create_agent
from backend.pipeline.model import groq_gpt_oss_20b

general_agent = create_agent(
    model=groq_gpt_oss_20b,
)
