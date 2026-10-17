"""Final answer call: entry point ``generate_answer`` maps the prompt onto a
validated ``AnswerOutput`` via direct ``deepseek.invoke`` (no ``response_format``
so reasoning streams; shape in ``ANSWER_JSON_SPEC``; errors propagate).
"""

import logging

from langchain_core.messages import HumanMessage, SystemMessage

from backend.pipeline.contracts import AnswerOutput
from backend.pipeline.models import deepseek
from backend.pipeline.prompts import ANSWER_SYSTEM_PROMPT

logger = logging.getLogger(__name__)

# The json output contract. It lives in the user message so the
# ``ANSWER_SYSTEM_PROMPT`` can stay the system message; no schema is
# transmitted, so the prompt must carry the word "json" and an explicit example
# of the shape. The system prompt already carries the writing/grounding rules,
# so this spec enumerates the two block shapes and the follow-up type.
ANSWER_JSON_SPEC = (
    "Return ONLY one json object and no other text, markdown, or commentary. "
    "The json object MUST match exactly this shape:\n"
    "{\n"
    '  "blocks": [\n'
    '    {"type": "text", "text": "one section of the answer as markdown prose"},\n'
    '    {"type": "table", "result_id": "R1"},\n'
    '    {"type": "text", "text": "the prose that follows the table"}\n'
    "  ],\n"
    '  "findings": [\n'
    '    {"id": "F1", "kind": "measured", "statement": "one claim", "result_ids": ["R1"]},\n'
    '    {"id": "F2", "kind": "hypothesis", "statement": "a possible cause", "result_ids": ["R2"]},\n'
    '    {"id": "F3", "kind": "limitation", "statement": "what the data does not show", "result_ids": []}\n'
    "  ],\n"
    '  "recommendations": [\n'
    '    {"action": "what to check or fix", "why": "why first, from the findings", '
    '"finding_ids": ["F1"], "owner_team": "Compute Platform"}\n'
    "  ],\n"
    '  "follow_up": null\n'
    "}\n\n"
    "Rules for filling the json:\n"
    "- Each `blocks` item is EXACTLY one of the two shapes shown: "
    '{"type": "text", "text": "..."} or {"type": "table", "result_id": "R#"}.\n'
    '- `type` is exactly "text" or "table"; no other value is valid.\n'
    "- A table's `result_id` must be an id from the tool result catalog in the "
    "user message; never invent an id.\n"
    "- `blocks` hold the role narrative only. Do NOT repeat the findings list, "
    "the recommendations, or a data-scope/query section in `blocks`: code renders "
    "those from the json fields.\n"
    '- Each finding `kind` is exactly "measured", "hypothesis" or "limitation". '
    '"measured" means the cited results directly show it and needs at least one '
    "`result_ids` entry; a possible cause is a \"hypothesis\"; what the data "
    'cannot establish is a "limitation". Numbers in a finding must appear in '
    "its cited results.\n"
    "- `recommendations`: one to three ordered, concrete next steps. `owner_team` "
    "comes from the team ownership mapping, or null.\n"
    '- `follow_up` is a string holding at most one short question, or null. Use '
    "null when no follow-up is warranted."
)


def generate_answer(prompt: str) -> AnswerOutput:
    """Build and return the validated answer for one answer prompt (built by
    ``prompts.answer`` or ``prompts.reframe``). Provider/pydantic failures are
    not caught and propagate to the caller."""
    message = deepseek.invoke(
        [
            SystemMessage(content=ANSWER_SYSTEM_PROMPT),
            HumanMessage(
                content=(
                    f"{prompt}\n\n{ANSWER_JSON_SPEC}\n\n"
                    "Produce the json answer now."
                )
            ),
        ]
    )
    usage = getattr(message, "usage_metadata", None) or {}
    logger.info(
        "answer model usage: input_tokens=%s output_tokens=%s",
        usage.get("input_tokens"),
        usage.get("output_tokens"),
    )
    return AnswerOutput.model_validate_json(message.text)


__all__ = ["generate_answer"]
