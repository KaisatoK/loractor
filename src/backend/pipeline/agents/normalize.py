"""Normalization call: prompt contract and entry point.

``graph.normalize_node`` turns the raw conversation into a validated
:class:`NormalizedOutput` that the rest of the pipeline consumes. This module
maps the supplied normalization prompt onto that contract with a direct
``deepseek.invoke`` call.

It owns exactly two things: the prompt/JSON contract that shapes the model's
output (``NORMALIZE_JSON_SPEC``) and the single public entry point
``generate_normalized``. Callers (``graph.py``'s ``normalize_node``) use that
entry point and nothing else. This module does not catch errors — provider or
pydantic validation failures propagate to the caller. The merge/rewrite
semantics live in ``NORMALIZE_SYSTEM_PROMPT``; the spec here only pins the json
shape and the field types the model cannot otherwise see.

The call deliberately passes no ``response_format`` — neither
``with_structured_output`` nor a ``response_format`` kwarg. Any structured-output
guarantee collapses the streamed reply to a single delta, so the live progress
panel could not render the model's reasoning token-by-token; without it the
projection streams incrementally. The prompt therefore carries the json shape
and ``NORMALIZE_JSON_SPEC``, and the returned message's text is validated here
with ``model_validate_json``. ``message.text`` is the text blocks only, so the
reasoning the model emits alongside the answer is excluded from the payload.
"""

from langchain_core.messages import HumanMessage, SystemMessage

from backend.pipeline.contracts import NormalizedOutput
from backend.pipeline.models import deepseek
from backend.pipeline.prompts import NORMALIZE_SYSTEM_PROMPT

# The json output contract. It lives in the user message so the
# ``NORMALIZE_SYSTEM_PROMPT`` can stay the system message; no schema is
# transmitted, so the prompt must carry the word "json" and an explicit example
# of the shape. The system prompt already carries the merge/rewrite semantics,
# so this spec pins the shape and the rules the example cannot convey.
NORMALIZE_JSON_SPEC = (
    "Return ONLY one json object and no other text, markdown, or commentary. "
    "The json object MUST match exactly this shape:\n"
    "{\n"
    '  "normalized_question": "concise, self-contained version of the newest question",\n'
    '  "intent": "free-form description of the user\'s desired outcome",\n'
    '  "context": {},\n'
    '  "ambiguities": []\n'
    "}\n\n"
    "Rules for filling the json:\n"
    "- All four keys are always present. `context` is a json object and may be "
    "`{}`; `ambiguities` is a json array of strings and may be empty. An empty "
    "`ambiguities` list (`[]`) is the common case.\n"
    "- Use an empty `ambiguities` list when the question is answerable as written.\n"
    "- Include an item in `ambiguities` ONLY when the request cannot be answered "
    "or investigated without resolving that uncertainty (for example a reference "
    "that cannot be resolved or a required detail that is genuinely missing). "
    'Only then does it look like ["unresolved uncertainty that must be resolved '
    'before answering"].\n'
    "- Never list optional details, preferences, or extra context as ambiguities.\n"
    "- When in doubt, use an empty list.\n"
    "- Copy facts only from the provided input; never invent information that is "
    "not present there.\n"
    "- Do not answer the question."
)


def generate_normalized(prompt: str) -> NormalizedOutput:
    """Build and return the validated normalization for one normalization prompt.

    Args:
        prompt: The normalization prompt built by ``prompts.normalize`` (the raw
            conversation or the previous-context/new-message blocks).

    Returns the :class:`NormalizedOutput` parsed from the model's text. A
    provider failure or a pydantic validation error is not caught and propagates
    to the caller.
    """
    message = deepseek.invoke(
        [
            SystemMessage(content=NORMALIZE_SYSTEM_PROMPT),
            HumanMessage(
                content=(
                    f"{prompt}\n\n{NORMALIZE_JSON_SPEC}\n\n"
                    "Produce the json normalization now."
                )
            ),
        ]
    )
    return NormalizedOutput.model_validate_json(message.text)


__all__ = ["generate_normalized"]
