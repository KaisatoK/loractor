from langchain.agents import create_agent

from backend.pipeline.contracts import NormalizedOutput
from backend.pipeline.middleware import make_error_middleware
from backend.pipeline.models import groq_gpt_oss_20b
from backend.pipeline.prompts import NORMALIZE_SYSTEM_PROMPT

normalize_agent = create_agent(
    model=groq_gpt_oss_20b,
    response_format=NormalizedOutput,
    middleware=[
        make_error_middleware(
            lambda message: NormalizedOutput.model_construct(
                normalized_question="",
                intent="",
                context={},
                ambiguities=[],
            )
        ),
    ],
    system_prompt=NORMALIZE_SYSTEM_PROMPT,
)
