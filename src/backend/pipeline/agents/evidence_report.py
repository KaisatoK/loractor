from langchain.agents import create_agent

from backend.pipeline.model import groq_gpt_oss_20b
from backend.pipeline.schema.schemas import EvidenceReport
from backend.pipeline.system_prompts import EVIDENCE_REPORT_SYSTEM_PROMPT
from backend.pipeline.utils import make_error_middleware


evidence_report_agent = create_agent(
    model=groq_gpt_oss_20b,
    response_format=EvidenceReport,
    middleware=[
        make_error_middleware(
            lambda message: EvidenceReport.model_construct(hypotheses=[])
        ),
    ],
    system_prompt=EVIDENCE_REPORT_SYSTEM_PROMPT,
)
