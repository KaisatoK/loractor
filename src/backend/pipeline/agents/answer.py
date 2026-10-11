from langchain.agents import create_agent

from backend.pipeline.contracts import AnswerOutput, AnswerText
from backend.pipeline.middleware import make_error_middleware
from backend.pipeline.models import groq_gpt_oss_120b
from backend.pipeline.prompts import ANSWER_SYSTEM_PROMPT

answer_agent = create_agent(
    model=groq_gpt_oss_120b,
    response_format=AnswerOutput,
    middleware=[
        make_error_middleware(
            lambda message: AnswerOutput.model_construct(
                blocks=[AnswerText.model_construct(type="text", text=message)]
            )
        ),
    ],
    system_prompt=ANSWER_SYSTEM_PROMPT,
)
