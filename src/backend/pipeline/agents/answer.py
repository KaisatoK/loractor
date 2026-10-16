"""Final answer call: prompt contract and entry point.

``graph.answer_node`` and ``api.chat_reframe`` need a validated
:class:`AnswerOutput` (an ordered list of prose and table blocks plus an
optional follow-up). This module maps the answer prompt onto that contract with
a direct ``deepseek.invoke`` call.

It owns exactly two things: the prompt/JSON contract that shapes the model's
output (``ANSWER_JSON_SPEC``) and the single public entry point
``generate_answer``. Callers (``graph.py``'s ``answer_node`` and
``api.chat_reframe``) use that entry point and nothing else. This module does not
catch errors — provider or pydantic validation failures propagate to the caller.
The writing and grounding semantics live in ``ANSWER_SYSTEM_PROMPT``; the spec
here only pins the json shape and block vocabulary the model cannot otherwise
see.

The call deliberately passes no ``response_format`` — neither
``with_structured_output`` nor a ``response_format`` kwarg. Any structured-output
guarantee collapses the streamed reply to a single delta, so the live progress
panel could not render the model's reasoning token-by-token; without it the
projection streams incrementally. The prompt therefore carries the json shape
and ``ANSWER_JSON_SPEC``, and the returned message's text is validated here with
``model_validate_json``. ``message.text`` is the text blocks only, so the
reasoning the model emits alongside the answer is excluded from the payload.
"""

from langchain_core.messages import HumanMessage, SystemMessage

from backend.pipeline.contracts import AnswerOutput
from backend.pipeline.models import deepseek
from backend.pipeline.prompts import ANSWER_SYSTEM_PROMPT

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
    """Build and return the validated answer for one answer prompt.

    Args:
        prompt: The answer prompt built by ``prompts.answer`` (or
            ``prompts.reframe`` for the reframe endpoint).

    Returns the :class:`AnswerOutput` parsed from the model's text. A provider
    failure or a pydantic validation error is not caught and propagates to the
    caller.
    """
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
