"""Normalization call: ``generate_normalized`` maps the prompt onto a validated
``NormalizedOutput`` via direct ``deepseek.invoke`` (no ``response_format`` so
reasoning streams; shape in ``NORMALIZE_JSON_SPEC``; errors propagate).
"""

from langchain_core.messages import HumanMessage, SystemMessage

from backend.pipeline.contracts import NormalizedOutput
from backend.pipeline.models import deepseek
from backend.pipeline.prompts import NORMALIZE_SYSTEM_PROMPT

# The json output contract, placed in the user message so the system prompt can
# stay ``NORMALIZE_SYSTEM_PROMPT``. No schema is transmitted, so it carries the
# word "json", a shape example, and the rules the example cannot convey.
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
    """Build and return the validated normalization for one prompt (built by
    ``prompts.normalize``). Provider or pydantic validation failures are not
    caught and propagate to the caller."""
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
