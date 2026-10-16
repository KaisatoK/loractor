"""Final answer call: entry point ``generate_answer`` maps the prompt onto a
validated ``AnswerOutput`` via direct ``deepseek.invoke`` (no ``response_format``
so reasoning streams; shape in ``ANSWER_JSON_SPEC``; errors propagate).
"""

from langchain_core.messages import HumanMessage, SystemMessage

from backend.pipeline.contracts import AnswerOutput
from backend.pipeline.models import deepseek
from backend.pipeline.prompts import ANSWER_SYSTEM_PROMPT

# The json output contract, placed in the user message so the system prompt can
# stay ``ANSWER_SYSTEM_PROMPT``. No schema is transmitted, so it carries the word
# "json", a shape example, and the two block shapes plus the follow-up type.
ANSWER_JSON_SPEC = (
    "Return ONLY one json object and no other text, markdown, or commentary. "
    "The json object MUST match exactly this shape:\n"
    "{\n"
    '  "blocks": [\n'
    '    {"type": "text", "text": "one section of the answer as markdown prose"},\n'
    '    {"type": "table", "result_id": "R1"},\n'
    '    {"type": "text", "text": "the prose that follows the table"}\n'
    "  ],\n"
    '  "follow_up": null\n'
    "}\n\n"
    "Rules for filling the json:\n"
    "- Each `blocks` item is EXACTLY one of the two shapes shown: "
    '{"type": "text", "text": "..."} or {"type": "table", "result_id": "R#"}.\n'
    '- `type` is exactly "text" or "table"; no other value is valid.\n'
    "- A table's `result_id` must be an id from the tool result catalog in the "
    "user message; never invent an id.\n"
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
    return AnswerOutput.model_validate_json(message.text)


__all__ = ["generate_answer"]
