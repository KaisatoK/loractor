from langchain.agents import create_agent

from backend.pipeline.contracts import EvidenceReport
from backend.pipeline.middleware import make_error_middleware

# from backend.pipeline.models import gemini_3_7_flash
from backend.pipeline.models import deepseek
from backend.pipeline.prompts import EVIDENCE_REPORT_SYSTEM_PROMPT

evidence_report_agent = create_agent(
    model=deepseek,
    response_format=EvidenceReport,
    middleware=[
        make_error_middleware(
            lambda message: EvidenceReport.model_construct(hypotheses=[])
        ),
    ],
    system_prompt=EVIDENCE_REPORT_SYSTEM_PROMPT,
)
