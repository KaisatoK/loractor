"""Evidence report call: prompt contract and entry point.

The ``find_evidence`` deep agent investigates and finishes with a free-text
summary; it does not emit structured data itself. This module maps that summary
plus the compact catalog of captured ``R``-ids onto a validated
:class:`EvidenceReport`, using a direct ``deepseek.invoke`` call.

It owns the prompt/JSON contract that shapes the model's output
(``JSON_OUTPUT_SPEC``) and the single public entry point
``generate_evidence_report``. Callers (``graph.py``'s ``find_evidence_node``)
use that entry point and nothing else. This module does not catch errors —
provider or pydantic validation failures propagate to the caller — and it
performs no citation filtering: the authoritative ``result_id`` validation
happens downstream in ``pipeline/answers.py`` and ``ui/rendering.py``.

The call deliberately passes no ``response_format`` — neither
``with_structured_output`` nor a ``response_format`` kwarg. Any structured-output
guarantee collapses the streamed reply to a single delta, so the live progress
panel could not render the model's reasoning token-by-token; without it the
projection streams incrementally. The JSON contract therefore rides in the
prompt text (``JSON_OUTPUT_SPEC``) instead of a transmitted schema — json mode
transmits no schema either, so the contract must enumerate every closed
vocabulary the model cannot otherwise see (for example ``tool_used[].status`` is
exactly ``"success"`` or ``"failure"``). The returned message's text is validated
here with ``model_validate_json``; ``message.text`` is the text blocks only, so
the reasoning the model emits alongside the report is excluded from the payload.
"""

from collections.abc import Sequence

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

from backend.pipeline.contracts import EvidenceReport
from backend.pipeline.models import deepseek
from backend.pipeline.prompts import EVIDENCE_REPORT_SYSTEM_PROMPT

# The json output contract. It lives in the user message so the
# ``EVIDENCE_REPORT_SYSTEM_PROMPT`` can stay the system message; no schema is
# transmitted, so the prompt must carry the word "json" and an explicit example
# of the shape. The system prompt already carries the grounding/fidelity rules,
# so this spec keeps the shape example and only the two rules the example
# cannot convey.
JSON_OUTPUT_SPEC = (
    "Return ONLY one json object and no other text, markdown, or commentary. "
    "The json object MUST match exactly this shape:\n"
    "{\n"
    '  "summary": "concise, evidence-grounded summary of the investigation",\n'
    '  "evidence": [\n'
    "    {\n"
    '      "id": "E1",\n'
    '      "name": "short evidence name",\n'
    '      "description": "why this evidence is needed and what it evaluates",\n'
    '      "tool_used": [\n'
    "        {\n"
    '          "name": "exact tool name",\n'
    '          "parameters": {"param": "value"},\n'
    '          "status": "success",\n'
    '          "error_message": null\n'
    "        }\n"
    "      ],\n"
    '      "availability": "available",\n'
    '      "result_ids": ["R1"]\n'
    "    }\n"
    "  ],\n"
    '  "hypotheses": [\n'
    "    {\n"
    '      "hypothesis": "specific, testable explanation",\n'
    '      "description": "why it is plausible and relevant",\n'
    '      "status": "supported",\n'
    '      "evidence_ids": ["E1"]\n'
    "    }\n"
    "  ]\n"
    "}\n\n"
    "Rules for filling the json:\n"
    "- `result_ids` must cite ONLY the `R`-ids that appear in the captured "
    "results catalog supplied below. Copy the id exactly. Never invent an id. "
    "Use an empty list when nothing was captured.\n"
    "- `tool_used[].status` is exactly \"success\" or \"failure\"; never "
    "\"failed\" or any other synonym. Set `error_message` to null whenever the "
    "status is \"success\".\n"
    "- Include `hypotheses` ONLY when the request actually required "
    "investigation (a why/what-is-causing question). For a direct data question "
    "(counts, lists, lookups) omit `hypotheses` or make it empty. Each "
    "hypothesis status is \"supported\", \"unsupported\", or \"inconclusive\"; "
    "use \"inconclusive\" whenever the evidence is missing, insufficient, "
    "ambiguous, or conflicting. Every `evidence_ids` entry must refer to an "
    "evidence item id that exists in the report."
)

PROMPT_TEMPLATE = (
    "<request>\n"
    "{request}\n"
    "</request>\n"
    "<role>\n"
    "{role}\n"
    "</role>\n"
    "<investigation summary>\n"
    "{investigation}\n"
    "</investigation summary>\n"
    "<captured results catalog>\n"
    "{catalog}\n"
    "</captured results catalog>\n\n"
    "{format}\n\n"
    "Produce the json evidence report now."
)


def _catalog_lines(results: Sequence[dict]) -> str:
    """Compact ``- R{n}: tool_name(arguments)`` lines for the captured results.

    Non-dict records are skipped; each record carries its own canonical ``id``.
    """
    return "\n".join(
        f"- {record['id']}: {record.get('tool_name')}({record.get('arguments')})"
        for record in results
        if isinstance(record, dict)
    )


def _messages(
    *, request: str, role: str, investigation: str, results: Sequence[dict]
) -> list[BaseMessage]:
    """Build the JSON-mode messages for one evidence-report call."""
    catalog = _catalog_lines(results)
    catalog = catalog.strip() or "(no results were captured)"
    prompt = PROMPT_TEMPLATE.format(
        request=request.strip(),
        role=role.strip(),
        investigation=investigation.strip() or "(no summary provided)",
        catalog=catalog,
        format=JSON_OUTPUT_SPEC,
    )
    return [
        SystemMessage(content=EVIDENCE_REPORT_SYSTEM_PROMPT),
        HumanMessage(content=prompt),
    ]


def generate_evidence_report(
    *, request: str, role: str, investigation: str, results: Sequence[dict]
) -> EvidenceReport:
    """Build and return the validated evidence report for one investigation.

    Args:
        request: The normalized user request (the question being answered).
        role: The user's role (name and objective) for tailoring.
        investigation: The investigator's free-text summary of the findings and
            the tools it used.
        results: The captured tool-result records, each carrying its canonical
            ``id``; those ids are the only ones the report may cite.

    Returns the :class:`EvidenceReport` parsed from the model's text. A provider
    failure or a pydantic validation error is not caught and propagates to the
    caller.
    """
    message = deepseek.invoke(
        _messages(
            request=request,
            role=role,
            investigation=investigation,
            results=results,
        )
    )
    return EvidenceReport.model_validate_json(message.text)


__all__ = [
    "generate_evidence_report",
]
