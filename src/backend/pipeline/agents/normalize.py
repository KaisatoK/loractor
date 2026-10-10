from langchain.agents import create_agent

from backend.pipeline.model import groq_gpt_oss_20b
from backend.pipeline.schema.schemas import NormalizedOutput
from backend.pipeline.system_prompts import NORMALIZE_SYSTEM_PROMPT
from backend.pipeline.utils import make_error_middleware


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
